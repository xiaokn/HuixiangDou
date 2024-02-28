# Copyright (c) OpenMMLab. All rights reserved.
"""extract feature and search with user query."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path

import numpy as np
import pytoml
from BCEmbedding.tools.langchain import BCERerank
from file_operation import FileOperation
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.retrievers import ContextualCompressionRetriever
from langchain.text_splitter import (MarkdownHeaderTextSplitter,
                                     MarkdownTextSplitter,
                                     RecursiveCharacterTextSplitter)
from langchain.vectorstores.faiss import FAISS as Vectorstore
from langchain_community.document_loaders import (
    CSVLoader, Docx2txtLoader, PyPDFLoader, UnstructuredExcelLoader,
    UnstructuredWordDocumentLoader)
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from loguru import logger
from sklearn.metrics import precision_recall_curve
from torch.cuda import empty_cache


class FeatureStore:
    """Tokenize and extract features from the project's documents, for use in
    the reject pipeline and response pipeline."""

    def __init__(self,
                 config_path: str = 'config.ini',
                 embeddings: HuggingFaceEmbeddings = None,
                 reranker: BCERerank = None,
                 language: str = 'zh') -> None:
        """Init with model device type and config."""
        self.config_path = config_path
        self.reject_throttle = -1
        self.language = language
        with open(config_path, encoding='utf8') as f:
            config = pytoml.load(f)['feature_store']
            self.reject_throttle = config['reject_throttle']

        logger.warning(
            '!!! If your feature generated by `text2vec-large-chinese` before 20240208, please rerun `python3 -m huixiangdou.service.feature_store`'  # noqa E501
        )

        logger.debug('loading text2vec model..')
        self.embeddings = embeddings
        self.reranker = reranker
        self.compression_retriever = None
        self.rejecter = None
        self.retriever = None
        self.md_splitter = MarkdownTextSplitter(chunk_size=768,
                                                chunk_overlap=32)
        self.text_splitter = RecursiveCharacterTextSplitter(chunk_size=768,
                                                            chunk_overlap=32)

        self.head_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[
            ('#', 'Header 1'),
            ('##', 'Header 2'),
            ('###', 'Header 3'),
        ])

    def is_chinese_doc(self, text):
        """If the proportion of Chinese in a bilingual document exceeds 0.5%,
        it is considered a Chinese document."""
        chinese_characters = re.findall(r'[\u4e00-\u9fff]', text)
        total_characters = len(text)
        ratio = 0
        if total_characters > 0:
            ratio = len(chinese_characters) / total_characters
        if ratio >= 0.005:
            return True
        return False

    def cos_similarity(self, v1: list, v2: list):
        """Compute cos distance."""
        num = float(np.dot(v1, v2))
        denom = np.linalg.norm(v1) * np.linalg.norm(v2)
        return 0.5 + 0.5 * (num / denom) if denom != 0 else 0

    def distance(self, text1: str, text2: str):
        """Compute feature distance."""
        feature1 = self.embeddings.embed_query(text1)
        feature2 = self.embeddings.embed_query(text2)
        return self.cos_similarity(feature1, feature2)

    def split_md(self, text: str, source: None):
        """Split the markdown document in a nested way, first extracting the
        header.

        If the extraction result exceeds 1024, split it again according to
        length.
        """
        docs = self.head_splitter.split_text(text)

        final = []
        for doc in docs:
            header = ''
            if len(doc.metadata) > 0:
                if 'Header 1' in doc.metadata:
                    header += doc.metadata['Header 1']
                if 'Header 2' in doc.metadata:
                    header += ' '
                    header += doc.metadata['Header 2']
                if 'Header 3' in doc.metadata:
                    header += ' '
                    header += doc.metadata['Header 3']

            if len(doc.page_content) >= 1024:
                subdocs = self.md_splitter.create_documents([doc.page_content])
                for subdoc in subdocs:
                    if len(subdoc.page_content) >= 10:
                        final.append('{} {}'.format(
                            header, subdoc.page_content.lower()))
            elif len(doc.page_content) >= 10:
                final.append('{} {}'.format(
                    header, doc.page_content.lower()))  # noqa E501

        for item in final:
            if len(item) >= 1024:
                logger.debug('source {} split length {}'.format(
                    source, len(item)))
        return final

    def clean_md(self, text: str):
        """Remove parts of the markdown document that do not contain the key
        question words, such as code blocks, URL links, etc."""
        # remove ref
        pattern_ref = r'\[(.*?)\]\(.*?\)'
        new_text = re.sub(pattern_ref, r'\1', text)

        # remove code block
        pattern_code = r'```.*?```'
        new_text = re.sub(pattern_code, '', new_text, flags=re.DOTALL)

        # remove underline
        new_text = re.sub('_{5,}', '', new_text)

        # remove table
        # new_text = re.sub('\|.*?\|\n\| *\:.*\: *\|.*\n(\|.*\|.*\n)*', '', new_text, flags=re.DOTALL)   # noqa E501

        # use lower
        new_text = new_text.lower()
        return new_text

    def get_md_documents(self, filepath):
        documents = []
        text = ''
        with open(filepath, encoding='utf8') as f:
            text = f.read()
        text = os.path.basename(filepath) + '\n' + self.clean_md(text)
        if len(text) <= 1:
            return []

        chunks = self.split_md(text=text, source=os.path.abspath(filepath))
        for chunk in chunks:
            new_doc = Document(page_content=chunk,
                               metadata={'source': os.path.abspath(filepath)})
            documents.append(new_doc)
        return documents

    def get_text_documents(self, text: str, filepath: str):
        if len(text) <= 1:
            return []
        chunks = self.text_splitter.create_documents([text])
        documents = []
        for chunk in chunks:
            chunk.metadata = {'source': filepath}
            documents.append(chunk)
        return documents

    def ingress_response(self, file_dir: str, work_dir: str):
        """Extract the features required for the response pipeline based on the
        document."""
        feature_dir = os.path.join(work_dir, 'db_response')
        if not os.path.exists(feature_dir):
            os.makedirs(feature_dir)

        files = [str(x) for x in list(Path(file_dir).glob('**/*'))]

        file_opr = FileOperation()
        documents = []
        for i, file in enumerate(files):
            basename = os.path.basename(file)
            logger.debug('{}/{}.. {}'.format(i+1, len(files), basename))
            file_type = file_opr.get_type(file)

            if file_type == 'md':
                documents += self.get_md_documents(file)
            else:
                text = file_opr.read(file)
                text = basename + text

                print(text)
                documents += self.get_text_documents(text, file)

        vs = Vectorstore.from_documents(documents, self.embeddings)
        vs.save_local(feature_dir)

    def ingress_reject(self, file_dir: str, work_dir: str):
        """Extract the features required for the reject pipeline based on
        documents."""
        feature_dir = os.path.join(work_dir, 'db_reject')
        if not os.path.exists(feature_dir):
            os.makedirs(feature_dir)

        files = [str(x) for x in list(Path(file_dir).glob('**/*'))]
        documents = []
        file_opr = FileOperation()

        for i, file in enumerate(files):
            logger.debug('{}/{}..'.format(i+1, len(files)))
            basename = os.path.basename(file)

            file_type = file_opr.get_type(file)
            if file_type == 'md':
                # reject base not clean md
                text = basename + '\n'
                with open(file, encoding='utf8') as f:
                    text += f.read()
                if len(text) <= 1:
                    continue

                chunks = self.split_md(text=text, source=os.path.abspath(file))
                for chunk in chunks:
                    new_doc = Document(
                        page_content=chunk,
                        metadata={'source': os.path.abspath(file)})
                    documents.append(new_doc)

            else:
                text = file_opr.read(file)
                text = basename + text
                documents += self.get_text_documents(text, file)

        vs = Vectorstore.from_documents(documents, self.embeddings)
        vs.save_local(feature_dir)

    def load_feature(self,
                     work_dir,
                     feature_response: str = 'db_response',
                     feature_reject: str = 'db_reject'):
        """Load extracted feature."""
        # https://api.python.langchain.com/en/latest/vectorstores/langchain.vectorstores.faiss.FAISS.html#langchain.vectorstores.faiss.FAISS

        resp_dir = os.path.join(work_dir, feature_response)
        reject_dir = os.path.join(work_dir, feature_reject)

        if not os.path.exists(resp_dir) or not os.path.exists(reject_dir):
            logger.error(
                'Please check README.md first and `python3 -m huixiangdou.service.feature_store` to initialize feature database'  # noqa E501
            )
            raise Exception(
                f'{resp_dir} or {reject_dir} not exist, please initialize with feature_store.'  # noqa E501
            )

        self.rejecter = Vectorstore.load_local(reject_dir,
                                               embeddings=self.embeddings)
        self.retriever = Vectorstore.load_local(
            resp_dir,
            embeddings=self.embeddings,
            distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT).as_retriever(
                search_type='similarity',
                search_kwargs={
                    'score_threshold': 0.2,
                    'k': 30
                })
        self.compression_retriever = ContextualCompressionRetriever(
            base_compressor=self.reranker, base_retriever=self.retriever)

    def preprocess(self, filepaths: list, work_dir: str):
        """Preprocesses markdown files in a given directory excluding those
        containing 'mdb'. Copies each file to 'preprocess' with new name formed
        by joining all subdirectories with '_'.

        Args:
            filepaths (list): Directory where the original markdown files reside.
            work_dir (str): Working directory where preprocessed files will be stored.  # noqa E501

        Returns:
            str: Path to the directory where preprocessed markdown files are saved.

        Raises:
            Exception: Raise an exception if no markdown files are found in the provided repository directory.  # noqa E501
        """
        file_dir = os.path.join(work_dir, 'preprocess')
        if os.path.exists(file_dir):
            logger.warning(
                f'{file_dir} already exists, remove and regenerate.')
            shutil.rmtree(file_dir)
        os.makedirs(file_dir)

        success_cnt = 0
        fail_cnt = 0
        skip_cnt = 0

        file_opr = FileOperation()

        for filepath in filepaths:
            try:
                _type = file_opr.get_type(filepath)
                if _type == 'image':
                    # TODO call multi-modal for OCR
                    pass
                elif _type in ['pdf', 'md', 'text', 'word', 'excel']:
                    basename = os.path.basename(filepath)
                    shutil.copy(filepath, os.path.join(file_dir, basename))
                    success_cnt += 1
                else:
                    skip_cnt += 1
                    logger.info(f'skip {filepath}')
            except Exception as e:
                fail_cnt += 1
                logger.error(str(e))

        logger.debug(
            f'preprocess input {len(filepaths)} files, {success_cnt} success, {fail_cnt} fail, {skip_cnt} skip. '
        )
        return file_dir, (success_cnt, fail_cnt, skip_cnt)

    def initialize(self, filepaths: list, work_dir: str):
        """Initializes response and reject feature store.

        Only needs to be called once. Also calculates the optimal threshold
        based on provided good and bad question examples, and saves it in the
        configuration file.
        """
        logger.info(
            'initialize response and reject feature store, you only need call this once.'  # noqa E501
        )
        file_dir, counter = self.preprocess(filepaths=filepaths,
                                            work_dir=work_dir)
        success_cnt, _, __ = counter
        if success_cnt > 0:
            self.ingress_response(file_dir=file_dir, work_dir=work_dir)
            self.ingress_reject(file_dir=file_dir, work_dir=work_dir)
            empty_cache()
        return counter


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Feature store for processing directories.')
    parser.add_argument('--work_dir',
                        type=str,
                        default='workdir',
                        help='Working directory.')
    parser.add_argument(
        '--repo_dir',
        type=str,
        default='repodir',
        help='Root directory where the repositories are located.')
    parser.add_argument(
        '--good_questions',
        default='resource/good_questions.json',
        help=  # noqa E251
        'Positive examples in the dataset. Default value is resource/good_questions.json'  # noqa E501
    )
    parser.add_argument(
        '--bad_questions',
        default='resource/bad_questions.json',
        help=  # noqa E251
        'Negative examples json path. Default value is resource/bad_questions.json'  # noqa E501
    )
    parser.add_argument(
        '--config_path',
        default='config.ini',
        help='Feature store configuration path. Default value is config.ini')
    parser.add_argument(
        '--sample', help='Input an json file, save reject and search output.')
    args = parser.parse_args()
    return args


def test_reject(sample: str = None):
    """Simple test reject pipeline."""
    if sample is None:
        real_questions = [
            '请问找不到libmmdeploy.so怎么办',
            'SAM 10个T 的训练集，怎么比比较公平呢~？速度上还有缺陷吧？',
            '想问下，如果只是推理的话，amp的fp16是不会省显存么，我看parameter仍然是float32，开和不开推理的显存占用都是一样的。能不能直接用把数据和model都 .half() 代替呢，相比之下amp好在哪里',  # noqa E501
            'mmdeploy支持ncnn vulkan部署么，我只找到了ncnn cpu 版本',
            '大佬们，如果我想在高空检测安全帽，我应该用 mmdetection 还是 mmrotate',
            'mmdeploy 现在支持 mmtrack 模型转换了么',
            '请问 ncnn 全称是什么',
            '有啥中文的 text to speech 模型吗?',
            '今天中午吃什么？',
            '茴香豆是怎么做的'
        ]
    else:
        with open(sample) as f:
            real_questions = json.load(f)
    fs_query = FeatureStore(config_path=args.config_path)
    fs_query.load_feature(work_dir=args.work_dir)
    for example in real_questions:
        reject, _ = fs_query.is_reject(example)

        if reject:
            logger.error(f'reject query: {example}')
        else:
            logger.warning(f'process query: {example}')

        if sample is not None:
            if reject:
                with open('workdir/negative.txt', 'a+') as f:
                    f.write(example)
                    f.write('\n')
            else:
                with open('workdir/positive.txt', 'a+') as f:
                    f.write(example)
                    f.write('\n')

    del fs_query
    empty_cache()


def test_query(sample: str = None):
    """Simple test response pipeline."""
    if sample is not None:
        with open(sample) as f:
            real_questions = json.load(f)
        logger.add('logs/feature_store_query.log', rotation='4MB')
    else:
        real_questions = ['mmpose installation']

    fs_query = FeatureStore(config_path=args.config_path)
    fs_query.load_feature(work_dir=args.work_dir)
    for example in real_questions:
        example = example[0:400]
        fs_query.query(example)
        empty_cache()

    del fs_query
    empty_cache()


if __name__ == '__main__':
    args = parse_args()

    if args.sample is None:
        # not test precision, build workdir
        fs_init = FeatureStore(config_path=args.config_path)
        with open(args.good_questions, encoding='utf8') as f:
            good_questions = json.load(f)
        with open(args.bad_questions, encoding='utf8') as f:
            bad_questions = json.load(f)

        filepaths = list(Path(args.repo_dir).glob('**/*'))

        fs_init.initialize(filepaths=filepaths, work_dir=args.work_dir)
        fs_init.update_throttle(good_questions=good_questions,
                                bad_questions=bad_questions)
        del fs_init

    test_reject(args.sample)
    test_query(args.sample)
