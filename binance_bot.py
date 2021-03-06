# -*- coding: utf-8 -*-
"""
币安自动交易bot
    KDJ + Bollinger Close Price边界跨过布林线后，通过KDJ的金叉给一个捕鱼信号。每次只出一单。
    只捕捉做多的信号，一次交易，最后是换成USDT，适合没有机器学习的场景
"""
import time
from datetime import datetime,timedelta
import sys
import signal
import threading

import logging
from logging.handlers import TimedRotatingFileHandler
import re

import pandas as pd
import talib
from talib import MA_Type
import numpy as np #computing multidimensionla arrays

from binance.client import Client
from binance.enums import *
from binance.websockets import BinanceSocketManager
from twisted.internet import reactor

from settings import MarginAccount
from settings import BinanceKey1
api_key = BinanceKey1['api_key']
api_secret = BinanceKey1['api_secret']
client = Client(api_key, api_secret, {"verify": True, "timeout": 10000})


# logger初始化

logger = logging.getLogger("kdj_bollinger_bot")

log_format = "%(asctime)s [%(threadName)s] [%(name)s] [%(levelname)s] %(filename)s[line:%(lineno)d] %(message)s"
log_level = 10
handler = TimedRotatingFileHandler("kdj_bollinger_bot.log", when="midnight", interval=1)
handler.setLevel(log_level)
formatter = logging.Formatter(log_format)
handler.setFormatter(formatter)
handler.suffix = "%Y%m%d"
handler.extMatch = re.compile(r"^\d{8}$") 
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# step0 初始化参数，请咨询核对调优
pair_symbol = MarginAccount['pair_symbol']
coin_symbol = MarginAccount['coin_symbol']
usdt_symbol = MarginAccount['usdt_symbol']
loan = MarginAccount['loan_balance']
depth = MarginAccount['depth']
coin_balance = MarginAccount['coin_balance']
# 每手交易的币数量。
qty = coin_balance / depth
base_balance = MarginAccount['base_balance']
# 最大交易对
max_margins = MarginAccount['max_margins']
# 账户余额必须大于30%才能交易
free_cash_limit_percentile = MarginAccount['free_cash_limit_percentile']
# 价格精度
price_accuracy = MarginAccount['price_accuracy']
# 数量进度
qty_accuracy = MarginAccount['qty_accuracy']
# 趋势判断值
trend_limit_tan = MarginAccount['trend_limit_tan']
#是否借币开关
loan_enabled = MarginAccount['loan_enabled']

# BinanceSocketManager 全局变量初始化
bm = None
conn_key = None
indicator = None
#memory order list
long_order = []
short_order = []
order_dt_started = datetime.utcnow()
close_price_list = []  #当前 close价格

def run():
    initialize_arb()

def initialize_arb():
    welcome_message = "\n\n---------------------------------------------------------\n\n"
    welcome_message+= "Binance auto trading bot\n"
    bot_start_time = str(datetime.now())
    welcome_message+= "\nBot Start Time: {}\n\n\n".format(bot_start_time)
    logger.info(welcome_message)
    time.sleep(1)
    status = client.get_system_status()
    logger.info("\nExchange Status: {}" % status)

    # step1 第一入口
    # 1.2 借出币
    if loan_enabled:
        loan_asset(coin_symbol, loan)

    # step2 监听杠杆交易
    global bm, conn_key
    bm = BinanceSocketManager(client)
    conn_key = bm.start_margin_socket(process_message)
    logger.info("websocket Conn key: {}".format(conn_key) )
    bm.setDaemon(True)
    bm.start()

    # 根据KDJ线 + Bolling线 定期执行, 主进程
    t = threading.Thread(target=kdj_signal_loop, args=(pair_symbol,))
    t.start()

    # 30分钟ping user websocket，key可以存活1个小时
    l = threading.Thread(target=get_margin_stream_keepalive, args=(conn_key,))
    l.setDaemon(True)
    l.start()

    # 30分钟检查一下过期的订单，主动取消。保证可以挂新订单。
    t = threading.Thread(target=outdated_order_clear, args=(pair_symbol,))
    t.setDaemon(True)
    t.start()


"""
超过区间日期（这里默认是3天）的订单自动取消
"""
def outdated_order_clear(symbol):
    while True:
        orders = client.get_open_margin_orders(symbol=symbol)
        today = datetime.now()
        offset = timedelta(days=-3)
        re_date = (today + offset)
        re_date_unix = time.mktime(re_date.timetuple())
        for o in orders: 
            d = float(o["time"])/1000
            #挂单时间小于等于3天前
            if d <= re_date_unix:
                result = client.cancel_margin_order(symbol=symbol,
                                            orderId=o["orderId"])
                logger.info("过期3天的订单自动取消，取消订单交易ID: {}".format(result))
        # 每个小时检查一次
        time.sleep(60*60*1)

"""
KDJ strategy loop
"""
def kdj_signal_loop(symbol):
    while True:
        kdj_signal_trading(symbol)
        time.sleep(5)

"""
 KDJ + bollinger_signal 自动交易策略
 bollinger_signal 21,2

 SHORT: 做空单， 1%
 LONG：做多单， 1%
 MLONG： 中间位置下跌空单， 0.5%
 MSHORT: 中间位置上涨， 0.5%
"""
def kdj_signal_trading(symbol):
    fastk_period = 9
    slowk_period = 3
    slowd_period = 3

    try:
        candles = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_5MINUTE)
        df = pd.DataFrame(candles)
        df.columns=['timestart','open','high','low','close','?','timeend','?','?','?','?','?']
        df.timestart = [datetime.fromtimestamp(i/1000) for i in df.timestart.values]
        df.timeend = [datetime.fromtimestamp(i/1000) for i in df.timeend.values]
    except Exception as e:
        logger.error("Connection aborted, {}".format(e))
        return

    low_list = df['low'].rolling(9, min_periods=9).min()
    low_list.fillna(value = df['low'].expanding().min(), inplace = True)
    high_list = df['high'].rolling(9, min_periods=9).max()
    high_list.fillna(value = df['high'].expanding().max(), inplace = True)

    # Close price
    close_data = [float(x) for x in df.close.values]
    np_close_data = np.array(close_data)
    rsv = (np_close_data - low_list) / (high_list - low_list) * 100

    #high price
    high_data = [float(x) for x in df.high.values]
    np_high_data = np.array(high_data)

    #low price
    low_data = [float(x) for x in df.low.values]
    np_low_data = np.array(low_data)

    #Bollinger range
    up,mid,down=talib.BBANDS(np_close_data,21,nbdevup=2,nbdevdn=2)
    cur_up = up[-1]
    cur_md = mid[-1]
    cur_dn = down[-1]

    logger.info("===================START=============================")
    logger.info("Bollinger: up: {:.2f}, mid: {:.2f}, down: {:.2f}".format(up[-1],mid[-1],down[-1]))

    df['K'] = pd.DataFrame(rsv).ewm(com=2).mean()
    df['D'] = df['K'].ewm(com=2).mean()
    df['J'] = 3 * df['K'] - 2 * df['D']
    np_k = np.array(df['K'])
    np_d = np.array(df['D'])
    np_j = np.array(df['J'])

    global indicator, long_order, short_order, order_dt_started, close_price_list
    logger.info("KDJ: k: {:.2f}, d: {:.2f}, j: {:.2f}".format(np_k[-1],np_d[-1],np_j[-1]))
    logger.info("K - D: {}".format(float(np_k[-1]) - float(np_d[-1])))
    logger.info("high_data: {},low_data: {}, close_data: {} => DN:{}, UP:{}".format(np_high_data[-1],np_low_data[-1],np_close_data[-1], cur_dn, cur_up))
    logger.info("LONG indicator: {}, long_order:{}".format(float(np_low_data[-1]) <= float(cur_dn), len(long_order)))
    logger.info("SHORT indicator: {}, short_order:{}".format(float(np_high_data[-1]) >= float(cur_up),len(short_order)))
    
    order_dt_ended = datetime.utcnow()
    logger.info("挂单时间间隔：{} > {}".format((order_dt_ended - order_dt_started).total_seconds(), 60*5))
    logger.info("===================END=============================")

    
    # 交易策略，吃多单
    if check_range(float(np_k[-1]) - float(np_d[-1]), -2.0, 2.0) and \
        float(np_k[-1]) < float(30) and float(np_j[-1]) < float(30) and \
        float(np_low_data[-1]) <= float(cur_dn)  and \
        (order_dt_ended - order_dt_started).total_seconds() > 60*5:

        indicator = "LONG"  # 做多
        close_price_list.append(float(np_close_data[-1]))  #加入当前最新价格
        order_dt_started = datetime.utcnow()  # 5分钟只能下一单
    elif check_range(float(np_k[-1]) - float(np_d[-1]), -2.0, 2.0) and \
         float(np_k[-1]) > float(70) and float(np_j[-1]) > float(70) and \
        float(np_high_data[-1]) >= float(cur_up)  and \
        (order_dt_ended - order_dt_started).total_seconds() > 60*5:

        indicator = "SHORT" # 做空
        close_price_list.append(float(np_close_data[-1]))  #加入当前最新价格
        order_dt_started = datetime.utcnow()  # 5分钟只能下一单
    elif check_range(float(np_k[-1]) - float(np_d[-1]), -2.0, 2.0) and \
         float(np_low_data[-1]) > float(cur_dn)  and \
         float(np_high_data[-1]) < float(cur_up)  and \
        (order_dt_ended - order_dt_started).total_seconds() > 60*5:

        indicator = "GRID" # 做网格
        close_price_list.append(float(np_close_data[-1]))  #加入当前最新价格
        order_dt_started = datetime.utcnow()  # 5分钟只能下一单

    # 这里加上延时判断，当获得信号后，判断60/5 = 12 次 信号中，close报价list是按照涨的趋势还是跌的趋势，这样可以果断修正交易策略
    if indicator in ["SHORT", "LONG", "GRID"]:
        close_price_list.append(float(np_close_data[-1]))  #加入当前最新价格
        logger.info("最新价格信号列表{}".format(close_price_list))
        # 开始计算close次数
        if len(close_price_list) >= 36:
            #判断list的数字趋势是涨还是跌
            indicator = check_indicator(close_price_list, indicator)
            logger.info("此次下单信号为: {}".format(indicator))
            new_margin_order(symbol,qty,indicator)  #  下单
            #重置条件，close_price_list.clear and indicator=NONE
            close_price_list.clear()
            indicator = None

"""
检查当前价格趋势
* k > 0.1763 表示上升
* k < -0.1763 表示下降
* 其他，则表示平稳或震荡
"""
def check_indicator(close_price_list, indicator):
    index=[*range(1,len(close_price_list)+1)]
    k=trendline(index, close_price_list, 3) #采用三次多项式拟合曲线
    logger.info("涨跌k趋势指标: {},  k > {} 表示上升, k < {} 表示下降".format(k, trend_limit_tan[0], trend_limit_tan[1]))
    if float(k) > float(trend_limit_tan[0]):  #上升 应该做多
        if indicator == "SHORT":
            indicator = "LONG"
    elif float(k) < float(trend_limit_tan[1]): #下降 应该做空
        if indicator == "LONG":
            indicator = "SHORT"
    #返回信号值
    return indicator

"""
最小二乘法将序列拟合成直线。后根据直线的斜率k去取得序列的走势
* k > 0.1763 表示上升
* k < -0.1763 表示下降
* 其他，则表示平稳或震荡
"""
def trendline(index,data, order=1):
    coeffs = np.polyfit(index, list(data), order)
    slope = coeffs[-2]
    return float(slope)

"""
检查K,D信号是否越界
"""
def check_range(value,left_tick,right_tick):
    if left_tick <= value <= right_tick:
        return True
    return False

"""
下单函数，做空，做多
"""
def new_margin_order(symbol,qty,indicator):
    # 当前报价口的买卖价格
    ticker = client.get_orderbook_ticker(symbol=symbol)
    logger.info("Current bid price: {}".format(ticker.get('bidPrice')))
    logger.info("Current ask price: {}".format(ticker.get('askPrice')))
    buy_price = float(ticker.get('bidPrice'))
    buy_price = price_accuracy % buy_price
    sell_price = float(ticker.get('askPrice'))
    sell_price = price_accuracy % sell_price

    # 挂单总数量给予限制
    if is_max_margins(max_margins) == True:
        logger.warning("Come across max margins limits....return, don't do order anymore.")
        return

    #计算当前账号的币的余额够不够，账户币余额必须大于free_cash_limit_percentile才能交易
    account = client.get_margin_account()
    userAssets = account.get('userAssets')
    free_coin = float(0)
    free_cash = float(0)
    for asset in userAssets:
        if asset.get('asset') == coin_symbol:
            free_coin = float(asset.get('free'))
        if asset.get('asset') == usdt_symbol:
            free_cash = float(asset.get('free'))
    # 规则：账户余额必须大于 base_balance * free_cash_limit_percentile 才能交易
    if free_coin < qty:
        logger.warning("Current Account coin balance < {} {}. don't do order anymore.".format(qty, coin_symbol))
        buy_coin_qty = float(qty_accuracy % float(loan * 0.5))
        if free_cash > (base_balance * free_cash_limit_percentile) and (buy_coin_qty * float(buy_price)) < free_cash:
            repay_asset(pair_symbol, coin_symbol, buy_coin_qty, "BUY")
        return
    if free_cash < (base_balance * free_cash_limit_percentile):
        logger.warning("Current Account cash balance < {}%. don't do order anymore.".format(free_cash_limit_percentile * 100))
        sell_coin_qty = float(qty_accuracy % float(free_coin - loan))
        if free_coin > loan and (sell_coin_qty * float(sell_price)) > 10:  # 币安要求最小交易金额必须大于10
            repay_asset(pair_symbol, coin_symbol, sell_coin_qty, SIDE_SELL)
        return

    # LONG or SHORT or GRID
    if indicator == "LONG":
        buy_price = float(ticker.get('bidPrice'))*float(1)
        buy_price = price_accuracy % buy_price

        sell_price = float(ticker.get('askPrice'))*float(1+0.008)
        sell_price = price_accuracy % sell_price

        buy_order = client.create_margin_order(symbol=symbol,
                                       side=SIDE_BUY,
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty,
                                       price=buy_price,
                                       sideEffectType='AUTO_REPAY',
                                       timeInForce=TIME_IN_FORCE_GTC)

        sell_order = client.create_margin_order(symbol=symbol,
                                       side=SIDE_SELL,
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty,
                                       price=sell_price,
                                       sideEffectType='MARGIN_BUY',
                                       timeInForce=TIME_IN_FORCE_GTC)

        logger.info("做多：买单ID:{}, 价格：{}， 数量：{}".format(buy_order, buy_price, qty))
        logger.info("做多：卖单ID:{}, 价格：{}， 数量：{}".format(sell_order, sell_price, qty)) 
        long_order.append( buy_order.get("orderId") )
        long_order.append( sell_order.get("orderId") )

    elif indicator == "GRID":
        buy_price = float(ticker.get('bidPrice'))*float(1-0.002)
        buy_price = price_accuracy % buy_price

        sell_price = float(ticker.get('askPrice'))*float(1+0.002)
        sell_price = price_accuracy % sell_price

        buy_order = client.create_margin_order(symbol=symbol,
                                       side=SIDE_BUY,
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty,
                                       price=buy_price,
                                       sideEffectType='AUTO_REPAY',
                                       timeInForce=TIME_IN_FORCE_GTC)

        sell_order = client.create_margin_order(symbol=symbol,
                                       side=SIDE_SELL,
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty,
                                       price=sell_price,
                                       sideEffectType='MARGIN_BUY',
                                       timeInForce=TIME_IN_FORCE_GTC)

        logger.info("做网格：买单ID:{}, 价格：{}， 数量：{}".format(buy_order, buy_price, qty))
        logger.info("做网格：卖单ID:{}, 价格：{}， 数量：{}".format(sell_order, sell_price, qty)) 
        short_order.append( buy_order.get("orderId") )
        short_order.append( sell_order.get("orderId") )

    elif indicator == "SHORT":
        buy_price = float(ticker.get('bidPrice'))*float(1-0.003)
        buy_price = price_accuracy % buy_price

        sell_price = float(ticker.get('askPrice'))*float(1)
        sell_price = price_accuracy % sell_price

        buy_order = client.create_margin_order(symbol=symbol,
                                       side=SIDE_BUY,
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty,
                                       price=buy_price,
                                       sideEffectType='AUTO_REPAY',
                                       timeInForce=TIME_IN_FORCE_GTC)

        sell_order = client.create_margin_order(symbol=symbol,
                                       side=SIDE_SELL,
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty,
                                       price=sell_price,
                                       sideEffectType='MARGIN_BUY',
                                       timeInForce=TIME_IN_FORCE_GTC)

        logger.info("做空：买单ID:{}, 价格：{}， 数量：{}".format(buy_order, buy_price, qty))
        logger.info("做空：卖单ID:{}, 价格：{}， 数量：{}".format(sell_order, sell_price, qty)) 
        short_order.append( buy_order.get("orderId") )
        short_order.append( sell_order.get("orderId") )
    else:
        logger.info("NO CHANCE: indicator:{}".format(indicator))

'''
purpose: coin补仓, 提供50%的币的数量
'''
def repay_asset(pair_symbol, coin_symbol, qty, type):
    ticker = client.get_orderbook_ticker(symbol=pair_symbol)
    print("Current bid price: {}".format(ticker.get('bidPrice')))
    print("Current ask price: {}".format(ticker.get('askPrice')))

    if type == SIDE_BUY:
        buy_price = float(ticker.get('bidPrice'))
        buy_price = price_accuracy % buy_price

        buy_order = client.create_margin_order(symbol=pair_symbol, 
                                       side=SIDE_BUY, 
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty, 
                                       price=buy_price,
                                       timeInForce=TIME_IN_FORCE_GTC)
        logger.info("自动补仓代币 {}: {}, 补仓单价：{}, order: {}".format(coin_symbol, qty, buy_price, buy_order))

    elif type ==  SIDE_SELL:
        sell_price = float(ticker.get('askPrice'))
        sell_price = price_accuracy % sell_price
    
        sell_order = client.create_margin_order(symbol=pair_symbol, 
                                       side=SIDE_SELL, 
                                       type=ORDER_TYPE_LIMIT,
                                       quantity=qty, 
                                       price=sell_price,
                                       timeInForce=TIME_IN_FORCE_GTC)
        logger.info("自动兑换代币 {}: {}, 兑换单价：{}, order: {}".format(coin_symbol, qty, sell_price, sell_order))

'''
purpose: 杠杆交易怕平仓，所以通过最简化的交易单数可以判断出是否超出仓位

'''
def is_max_margins(max_margins):
    orders = client.get_open_margin_orders(symbol=pair_symbol)
    if len(orders) > max_margins:
        return True
    else:
        return False

'''
purpose: 自动借币

if account.loan 有币:
    pass
'''
def loan_asset(coin_symbol, qty):
    account = client.get_margin_account()
    userAssets = account.get('userAssets')
    origin_loan = float(0)
    for asset in userAssets:
        if asset.get('asset') == coin_symbol:
            origin_loan = float(asset.get('borrowed'))
    qty = qty - origin_loan
    if qty <= float(0):
        logger.info('don\'t need loan, original loan: {}'.format(origin_loan))
        pass
    else:
        transaction = client.create_margin_loan(asset=coin_symbol,
                                            amount=qty)
        logger.info(transaction)

def process_message(msg):
    if msg['e'] == 'error':
        error_msg = "\n\n---------------------------------------------------------\n\n"
        error_msg += "websocket error:\n"
        error_msg += msg.get('m')
        logger.warning(error_msg)
        # close and restart the socket
        global conn_key
        bm.stop_socket(conn_key)
        conn_key = bm.start_margin_socket(process_message)
        logger.info("renewer websocket Conn key: {}".format(conn_key))
    else:
        # 处理event executionReport
        if msg.get('e') == 'executionReport' and msg.get('s')  == pair_symbol:
            logger.info(msg)
        # 当有交易成功的挂单，更新交易对字典
        # "i": 4293153,                  // orderId
        if msg.get('e') == 'executionReport' and msg.get('s')  == pair_symbol and msg.get('X') == ORDER_STATUS_FILLED:
            global short_order, long_order
            if msg.get('i') in short_order:
                short_order.remove( msg.get('i') )
                logger.info("做空单交易完成，交易ID: {}".format(msg.get('i')))
            elif msg.get('i') in long_order:
                long_order.remove( msg.get('i') )
                logger.info("做多单交易完成，交易ID: {}".format(msg.get('i')))

'''
Purpose: Keepalive a user data stream to prevent a time out.
User data streams will close after 60 minutes.
It's recommended to send a ping about every 30 minutes.
'''
def get_margin_stream_keepalive(listen_key):
    while True:
        result = client.margin_stream_keepalive(listen_key)
        time.sleep(60 * 30)

def term_sig_handler(signum, frame):
    print('catched singal: %d' % signum)
    reactor.stop()
    sys.exit()

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, term_sig_handler)
    signal.signal(signal.SIGINT, term_sig_handler)
    signal.signal(signal.SIGHUP, term_sig_handler)
    run()