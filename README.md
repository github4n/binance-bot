## QCat Automated Crypto Trading bot

### *注意* ：对于小于4G内存的小机器，增加Swap
RAM > 1G or add swap， 不然ta-lib安装会失败

```bash
# (增加4G)
dd if=/dev/zero of=/tmp/mem.swap bs=1M count=4096
free -m

mkswap /tmp/mem.swap
swapon /tmp/mem.swap
# 确认是否增加成功：
free -m
```

### 安装RSI指标计算库依赖
```bash
apt-get update
apt-get install build-essential

# download from 
wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz

tar -xvf ta-lib-0.4.0-src.tar.gz

cd ta-lib

./configure --prefix=/usr

make && make install

apt-get install python3-pip
### ta-lib, 需要最少1G的内存，小机器请使用swap扩展
pip3 install numpy -i http://pypi.douban.com/simple --trusted-host pypi.douban.com
pip3 install ta-lib

pip3 install pandas -i http://pypi.douban.com/simple --trusted-host pypi.douban.com
pip3 install -r requirements.txt
```

### 安装配置并运行
bash setup.sh #初始化安装
vi settings.py   #bot全局配置，运行前务必配置
start.sh

### 打包发行安装包
bash bundle.sh 生成安装包并发行zip格式安装包

* start.sh  启动机器人
* stop.sh  暂停机器人
* top.sh  查询当前账户挂单状态
* mail.sh  发盈利报告邮件，12小时统计发送一次
* mailstop.sh  停止邮件

### How do I protect my Python source code?

在线混淆工具(选择：Dancing Links模式)：http://pyob.oxyry.com/

专业工具(Linux，macos)：https://github.com/Hnfull/Intensio-Obfuscator

