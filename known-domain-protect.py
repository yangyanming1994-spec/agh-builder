#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# known-domain-protect.py v1.0
# 知名业务域自动保护: 若知名业务域被父域级规则整体拦截,自动放行父域+保留其广告子域拦截
# 放行父域 -> local/wan.txt (白名单源) ; 广告子域转important -> local/hardblock.txt (强制黑名单源)
import re, os, subprocess, sys, datetime

# ---- 知名业务域清单(全球+国内主流,可自行扩充) ----
KNOWN = """google.com youtube.com facebook.com instagram.com twitter.com x.com amazon.com netflix.com openai.com github.com microsoft.com apple.com zoom.us discord.com telegram.org whatsapp.com wikipedia.org reddit.com tiktok.com linkedin.com spotify.com cloudflare.com dropbox.com notion.so figma.com slack.com disneyplus.com hbomax.com paramountplus.com bbc.com cnn.com nytimes.com epicgames.com blizzard.com riotgames.com battle.net steampowered.com ebay.com paypal.com yahoo.com bing.com duckduckgo.com pinterest.com tumblr.com medium.com substack.com twitch.com gitlab.com bitbucket.org docker.com npmjs.com pypi.org stripe.com visa.com mastercard.com hulu.com vimeo.com shopify.com etsy.com aliexpress.com stackoverflow.com quora.com khanacademy.org coursera.org udemy.com nintendo.com xbox.com ea.com ubisoft.com reuters.com bloomberg.com forbes.com wsj.com msn.com skype.com openstreetmap.org adobe.com oracle.com ibm.com salesforce.com intel.com nvidia.com amd.com samsung.com sony.com lg.com verizon.com uber.com airbnb.com booking.com expedia.com tripadvisor.com qq.com wechat.com taobao.com tmall.com alipay.com alibaba.com douyin.com bytedance.com jd.com baidu.com weibo.com zhihu.com bilibili.com xiaohongshu.com meituan.com didiglobal.com 163.com iqiyi.com youku.com douban.com sohu.com 12306.cn ctrip.com qunar.com suning.com zuche.com kuaishou.com ximalaya.com mogujie.com 51job.com zhaopin.com huya.com douyu.com ele.me didi.com vip.com pinduoduo.com yangkeduo.com dangdang.com guazi.com che168.com autohome.com.cn ganji.com 58.com lianjia.com fang.com hao123.com xiaomi.com mi.com huawei.com honor.com vivo.com.cn oppo.com lenovo.com dell.com hp.com zte.com.cn h3c.com cctv.com netease.com aliyun.com tencent.com sina.com.cn sf-express.com yto.net.cn zto.com tuniu.com mafengwo.com elong.com lvmama.com csdn.net juejin.com jianshu.com renren.com fenqile.com so.com sogou.com 360.cn mgtv.com yhd.com dianping.com lagou.com ke.com amap.com dongchedi.com yiche.com gitee.com oschina.net dewu.com zhuanzhuan.com huolala.cn 10086.cn chinaunicom.com chinatelecom.com.cn 189.cn boc.cn icbc.com.cn ccb.com abchina.com cmbchina.com bankcomm.com psbc.com cebbank.com cib.com.cn spdb.com.cn pingan.com china-life.com.cn picc.com.cn cpic.com.cn dxy.cn haodf.com gov.cn 12123.gov.cn mps.gov.cn chinanews.com people.com.cn xinhuanet.com cnr.cn ce.cn china.com.cn hunantv.com miguvideo.com cmbc.com.cn hxb.com.cn cgbchina.com.cn pinganbank.com.cn liepin.com zhipin.com xueersi.com zuoyebang.com yuanfudao.com guahao.com ifeng.com thepaper.cn guancha.cn caixin.com yicai.com anjuke.com pcauto.com.cn xcar.com.cn lu.com mybank.com qy.net pddpic.com mof.gov.cn mofcom.gov.cn miit.gov.cn ndrc.gov.cn sasac.gov.cn nhsa.gov.cn hsbc.com.hk standardchartered.com citibank.com jpmorganchase.com bankofamerica.com dbs.com uob.com.sg ocbc.com maybank.com taikang.com newchinalife.com aia.com tsinghua.edu.cn pku.edu.cn fudan.edu.cn zju.edu.cn sjtu.edu.cn nju.edu.cn whu.edu.cn hust.edu.cn sysu.edu.cn dji.com byd.com nio.com xiaopeng.com lixiang.com tesla.com geely.com gwm.com.cn chery.com yonyou.com kingdee.com inspur.com kingsoft.com wind.com.cn fliggy.com tongcheng.com mobike.com hellobike.com huxiu.com 36kr.com ithome.com gamersky.com qidian.com jjwxc.net smzdm.com jumei.com quark.cn weiyun.com jianguoyun.com wps.cn feishu.cn yinxiang.com shimo.im kdocs.cn dingtalk.com aliyundrive.com maimai.cn dajie.com yingjiesheng.com lilith.com hypergryph.com disney.com warnerbros.com spacex.com nasa.gov leetcode.com nowcoder.com segmentfault.com yy.com 17k.com kugou.com kuwo.cn duomi.com qqmusic.com meipai.com keep.com zhangmen.com biying.com 114.com yundaex.com sto.cn jtexpress.com ems.com.cn cainiao.com debanglogistics.com citics.com cmschina.com gtja.com htsec.com gf.com.cn chinastock.com.cn eastmoney.com 10jqka.com.cn xueqiu.com futunn.com 5i5j.com vanke.com countrygarden.com.cn poly.com.cn longfor.com sunac.com.cn faw.com.cn saicmotor.com dfmc.com.cn changan.com.cn gac.com.cn jac.com.cn goofish.com dew.com weidian.com jetbrains.com vercel.com netlify.com digitalocean.com linode.com signal.org nankai.edu.cn tju.edu.cn hit.edu.cn xjtu.edu.cn tongji.edu.cn buaa.edu.cn bit.edu.cn ecnu.edu.cn seu.edu.cn xmu.edu.cn uestc.edu.cn 17zuoye.com hujiang.com shanbay.com baicizhan.com liulishuo.com youdao.com pfizer.com jnj.com roche.com novartis.com astrazeneca.com bayer.com msd.com hengrui.com bankofbeijing.com.cn bosc.cn nbcb.com.cn njcb.com.cn jsbank.com.cn hzbank.com.cn webank.com unionpay.com tenpay.com lianlianpay.com lakala.com agoda.com trip.com caocaomobility.com shouqiev.com pptv.com ixigua.com migu.cn qingting.fm lizhi.fm huangiu.com oeeee.com infzm.com jiemian.com 21jingji.com tmtpost.com glodon.com foxitsoftware.com autodesk.com sap.com qualcomm.com panasonic.com canon.com nikon.com asus.com acer.com razer.com logitech.com philips.com siemens.com ge.com pwrd.com youzu.com seasun.com xd.com coca-cola.com pepsi.com nike.com adidas.com starbucks.com mcdonalds.com kfc.com walmart.com costco.com ikea.com muji.com yili.com mengniu.com.cn gree.com.cn midea.com haier.com tcl.com skyworth.com.cn hisense.com konka.com airchina.com.cn csair.com ceair.com hainanairlines.com ch.com juneyaoair.com xiamenair.com huazhu.com homeinns.com hilton.com marriott.com ihg.com shangri-la.com hyatt.com accor.com sgcc.com.cn csg.cn cnpc.com.cn sinopec.com cnooc.com.cn nba.com realmadrid.com fcbarcelona.com manutd.com anta.com lining.com xtep.com.cn luogu.com.cn codeforces.com atcoder.jp kaggle.com huggingface.co v2ex.com""".split()

# ---- 广告/追踪关键词(判断子域是否广告) ----
AD_KW = ["ad","ads","adsystem","adnxs","doubleclick","googlesyndication","googletagmanager","track",
         "tracking","analytics","metric","stat","pixel","count","counter","click","impression",
         "criteo","taboola","outbrain","rubicon","appnexus","moat","scorecardresearch","beacon",
         "telemetry","spy","cookie","collect","pageview","hotjar","advert","promo","sponsor","yield",
         "bidder","pubmatic","openx","smartadserver","mgid","adserve","adservice","adsrv","adserver",
         "adtrack","telemetry","iad","searchads","adpartner","adtech","adlog","log-sdk","ulog","mobads"]

DIST_RULES  = "/opt/agh-builder/dist/adguard-home-rules.txt"
DIST_ALLOW  = "/opt/agh-builder/dist/adguard-home-allowlist.txt"
WAN         = "/opt/agh-builder/local/wan.txt"
HARDBLOCK   = "/opt/agh-builder/local/hardblock.txt"
BUILD_SH    = "/usr/local/bin/build-and-refresh.sh"
LOG         = "/var/log/known-domain-protect.log"

def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a") as f: f.write(line + "\n")
    except Exception: pass

def is_registered(dom):
    parts = dom.split(".")
    PSL2 = set(["com.cn","net.cn","org.cn","gov.cn","edu.cn","ac.cn","com.hk","org.hk","net.hk",
        "com.tw","org.tw","net.tw","com.sg","com.my","com.mx","co.uk","org.uk","net.uk","co.jp",
        "ne.jp","or.jp","com.au","net.au","org.au","co.nz","org.nz","co.kr","or.kr","com.br",
        "net.br","com.ar","com.tr","com.ph","com.vn","com.ua","com.pl","com.ru","co.id","com.id",
        "com.eg","com.sa","co.in","net.in","com.in","com.co"])
    if len(parts) == 2: return True
    if len(parts) == 3 and (parts[-2]+"."+parts[-1]) in PSL2: return True
    return False

def main():
    KNOWN_SET = set(d.lower() for d in KNOWN if "." in d)
    # 读当前被整体拦的注册域
    blocked = set()
    try:
        for line in open(DIST_RULES):
            m = re.match(r"^\|\|([a-z0-9\-\.]+)\^$", line.strip())
            if m and "*" not in m.group(1) and is_registered(m.group(1)):
                blocked.add(m.group(1))
    except Exception as e:
        log("读取黑名单失败: %s" % e); return 1
    # 读已白名单放行的知名域
    allowed = set()
    try:
        for line in open(DIST_ALLOW):
            m = re.match(r"^@@\|\|([a-z0-9\-\.]+)\^$", line.strip())
            if m: allowed.add(m.group(1))
    except Exception: pass
    # 读 wan.txt 已加白(可能构建后未刷新,防止重复加)
    wan_set = set()
    try:
        for line in open(WAN):
            m = re.match(r"^@@\|\|([a-z0-9\-\.]+)\^$", line.strip())
            if m: wan_set.add(m.group(1))
    except Exception: pass

    to_protect = [k for k in KNOWN_SET if k in blocked and k not in allowed and k not in wan_set]
    if not to_protect:
        log("无需变更: %d 个知名域均未整体被拦" % len(KNOWN_SET))
        return 0

    log("发现 %d 个知名业务域被整体拦截,开始保护: %s" % (len(to_protect), ",".join(to_protect)))
    # 收集所有子域规则
    sub_rules = {}
    for line in open(DIST_RULES):
        m = re.match(r"^\|\|([a-z0-9\-\.]+)\^$", line.strip())
        if m:
            dom = m.group(1)
            for k in to_protect:
                if dom.endswith("."+k):
                    sub_rules.setdefault(k, []).append(dom)

    for k in to_protect:
        # 1) 放行父域到 wan.txt
        with open(WAN, "a") as f: f.write("@@||%s^\n" % k)
        log("  放行父域: @@||%s^" % k)
        # 2) 该域下广告子域转 important 到 hardblock(放行父域后仍强制拦截广告)
        ad_subs = [s for s in sub_rules.get(k, []) if any(kw in s for kw in AD_KW)]
        for s in ad_subs:
            with open(HARDBLOCK, "a") as f: f.write("||%s^$important\n" % s)
        log("  保留广告子域拦截 %d 条(%s)" % (len(ad_subs), ",".join(ad_subs[:3])+("..." if len(ad_subs)>3 else "")))

    log("已更新 wan.txt/hardblock.txt,触发重建...")
    subprocess.call(["nice","-n","19","ionice","-c3",BUILD_SH])
    log("重建完成")
    return 0

if __name__ == "__main__":
    sys.exit(main())
