import { useNavigate } from 'react-router-dom';
import { useLocale } from '@hooks/useLocale';
import { Input, message } from 'sea-lion-ui';
import { useEffect, useMemo, useState } from 'react';
import logo from '@assets/imgs/logo.png';
import {
    getStatistic, loginBean, MsgCode, StatisticDto
} from '@services/home';
import styles from './home.module.less';

const Home = () => {
    const navigate = useNavigate();
    const [beanName, setBeanName] = useState('');
    const [beanPwd, setBeanPwd] = useState('');
    const [existed, setExisted] = useState(false);
    const [statistic, setStatistic] = useState<StatisticDto>(null);
    const locales = useLocale('home');

    const resetInput = () => {
        setBeanName('');
        setBeanPwd('');
    };

    const validateBean = async (name, password) => {
        const res = await loginBean(name, password);
        if (res.msgCode !== MsgCode.success) {
            message.error(res.msg);
        }
        if (res.msgCode === MsgCode.success && res.data.featureStoreId) {
            navigate(`/bean-detail/?bean=${res.data.featureStoreId}`);
        }
    };

    const handleConfirm = () => {
        if (beanName && beanName.length < 8) {
            message.info('知识库名称不少于 8 个字符');
            return;
        }
        if (beanName && beanName.length > 7 && beanPwd) {
            validateBean(beanName, beanPwd);
        }
    };

    useEffect(() => {
        (async () => {
            const res = await getStatistic();
            if (res) {
                setStatistic(res);
            }
        })();
    }, []);

    const Statistics = useMemo(() => {
        if (!statistic) return [];
        return ([
            {
                title: locales.bean,
                key: 'bean',
                number: statistic.qalibTotal || 0
            },
            {
                title: locales.WeChat,
                key: 'WeChat',
                number: statistic.wechatTotal || 0
            },
            {
                title: locales.users,
                key: 'users',
                number: statistic.servedTotal || 0
            },
            {
                title: locales.activeBean,
                key: 'activeBean',
                number: statistic.lastMonthUsed || 0
            },
            {
                title: locales.feishu,
                key: 'feishu',
                number: statistic.feishuTotal || 0
            },
            {
                title: locales.uniqueUsers,
                key: 'uniqueUsers',
                number: statistic.realServedTotal || 0
            }
        ]);
    }, [locales, statistic]);

    return (
        <div className={styles.home}>
            <div className={styles.wrapper}>
                <div className={styles.logo}>
                    <img src={logo} alt="huixiangdou" />
                </div>
                <div className={styles.slogan}>行业知识助手，零开发接入飞书微信</div>
                <div className={styles.inputWrapper}>
                    <Input
                        placeholder={locales.beanName}
                        value={beanName}
                        max={500}
                        onChange={(e) => setBeanName(e.target.value)}
                    />
                    <Input
                        placeholder={locales.beanPwd}
                        value={beanPwd}
                        max={500}
                        onChange={(e) => setBeanPwd(e.target.value)}
                    />
                </div>
                <div className={styles.btnWrapper}>
                    <div
                        className={styles.cancelBtn}
                        onClick={resetInput}
                    >
                        {locales.cancel}
                    </div>
                    <div
                        className={styles.btn}
                        onClick={handleConfirm}
                        aria-disabled={!beanName || !beanPwd}
                    >
                        {existed ? locales.create : locales.go}
                    </div>
                </div>
                <div className={styles.divider} />
                <div className={styles.statisticsWrapper}>
                    {Statistics.map((item) => (
                        <div
                            key={item.key}
                            className={styles.statisticsItem}
                        >
                            <div className={styles.statisticsItemTitle}>{item.title}</div>
                            <div className={styles.statisticsItemNumber}>{item.number}</div>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
};

export default Home;
