import os
import sys
import math
import time
import logging
import asyncio
import traceback
import numpy
import ccxt.async_support as ccxt
dir_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(dir_root)
import db
from exchange.exchange_trade import exchange_trade
from exchange.ex_util import *
import util
logger = util.get_log(__name__)


# 搬砖
class stat_arbitrage():
    def __init__(self, symbol, ex1, ex2):
        self.symbol = symbol    # BTC/USD
        self.ex1 = ex1
        self.ex2 = ex2
    
        self.base = self.symbol.split('/')[0]   # BTC
        self.quote = self.symbol.split('/')[1]  # USD

        self.spread1List = []       # exchange1_buy1 - exchange2_sell1
        self.spread2List = []       # exchange2_buy1 - exchange1_sell1

        # 多少根k线, 计算的时间跨度. 默认1秒1k线，计算1小时
        #self.sma_window_size = 3600
        self.sma_window_size = 300   # for debug

        self.spread1_mean = None    # 均值
        self.spread1_stdev = None   # 方差
        self.spread2_mean = None
        self.spread2_stdev = None

        # 价格超过方差多少倍，有套利机会，下单
        self.spread1_open_condition_stdev_coe = 2.0
        self.spread2_open_condition_stdev_coe = 2.0

        # 价格小于方差多少倍，没有套利机会，这时可以考虑调整仓位
        self.spread1_close_condition_stdev_coe = 0.3
        self.spread2_close_condition_stdev_coe = 0.3

        # 0: no position
        # 1: long spread1(sell exchange1, buy exchange2)
        # 2: long spread2(buy exchange1, sell exchange2)
        self.current_position_direction = 0

        self.spread1_pos_amount = 0.0
        self.spread2_pos_amount = 0.0

        # 并不是所有挂单都能成交，每次预计能吃到的盘口深度的百分比
        self.order_book_ratio = 0.3

        # 评估 最小下注
        self.amount_min = 0.0

        # 仓位 再平衡，开关
        self.rebalance_on = False

        #load_markets_db(self.ex1.ex.id)
        #load_markets_db(self.ex2.ex.id)
        if symbol not in g_ex_markets[self.ex1.ex.id].keys() or symbol not in g_ex_markets[self.ex2.ex.id].keys():
            raise Exception("stat_arbitrage() symbol error")


    async def close(self):
        await self.ex1.close()
        await self.ex2.close()
        
    def to_string(self):
        return "stat_arbitrage[{0}:{1},{2}]".format(self.symbol, self.ex1.ex.id, self.ex2.ex.id)
    
    '''
    # 刚启动时，没有k线数据，尝试从数据库中取数据
    def fetch_history_data_from_db(self):
        logger.debug(self.to_string() + 'fetch_history_data_from_db() start')
        if len(self.spread1List) > 0:
            return
        # 取最近的 sma_window_size 个k线
        from_dt = int(time.time()) - self.sma_window_size * 2
        for i in range(from_dt, from_dt + self.sma_window_size):
            self.fetch_data_from_db(i)
        logger.debug(self.to_string() + 'fetch_history_data_from_db() end')
    
    # 从 db 中取1条指定时间的数据
    def fetch_data_from_db(self, sql_con_timestamp):
        rows1 = self.db.ticker_select(self.db.db_name, self.symbol, self.ex1.ex.id, sql_con_timestamp)
        rows2 = self.db.ticker_select(self.db.db_name, self.symbol, self.ex2.ex.id, sql_con_timestamp)
        if rows1 is None or rows2 is None:
            logger.debug(self.to_string() + 'fetch_data_from_db() return rows1 is None or rows2 is None')
            return
        if len(rows1) >= 1:
            for row1 in rows1:
                self.ex1.buy_1_price = row1[3]
                self.ex1.sell_1_price = row1[4]
                self.ex1.order_book_time = sql_con_timestamp
        if len(rows2) >= 1:
            for row2 in rows2:
                self.ex2.buy_1_price = row2[3]
                self.ex2.sell_1_price = row2[4]
                self.ex2.order_book_time = sql_con_timestamp
        if self.ex1.buy_1_price <= 0 or self.ex1.sell_1_price <= 0 or self.ex2.buy_1_price <= 0 or self.ex2.sell_1_price <= 0:
            logger.debug(self.to_string() + 'fetch_data_from_db() return price error')
            return
        spread1 = self.ex1.buy_1_price - self.ex2.sell_1_price
        spread2 = self.ex2.buy_1_price - self.ex1.sell_1_price
        self.spread1List = self.add_to_list(self.spread1List, spread1)
        self.spread2List = self.add_to_list(self.spread2List, spread2)
    '''

    def add_async_task(self):
        tasks = []
        tasks.append(asyncio.ensure_future(self.ex1.run(self.ex1.fetch_order_book, self.symbol)))
        tasks.append(asyncio.ensure_future(self.ex2.run(self.ex2.fetch_order_book, self.symbol)))
        tasks.append(asyncio.ensure_future(self.run(self.run_arbitrage)))
        return tasks

    def fetch_order_book(self):
        if self.ex1.order_book_time <= self.ex1.order_book_time_pre or self.ex2.order_book_time <= self.ex2.order_book_time_pre:
            return False
        if abs(self.ex1.order_book_time - self.ex2.order_book_time) > 3:
            logger.debug(self.to_string() + 'fetch_order_book() 时间不同步')
            return False
        self.ex1.order_book_time_pre = self.ex1.order_book_time
        self.ex2.order_book_time_pre = self.ex2.order_book_time
        # 加入数据列表
        spread1 = self.ex1.buy_1_price - self.ex2.sell_1_price
        spread2 = self.ex2.buy_1_price - self.ex1.sell_1_price
        self.spread1List = self.add_to_list(self.spread1List, spread1)
        self.spread2List = self.add_to_list(self.spread2List, spread2)
        return True

    # 向list添加1个元素，当list长度大于某个定值时，会将前面超出的部分删除
    def add_to_list(self, dest_list, element):
        if self.sma_window_size == 1:
            return [element]
        while len(dest_list) > self.sma_window_size:
            del (dest_list[0])
        dest_list.append(element)
        return dest_list

    # 初始化足够的数据
    async def init_data(self):
        '''
        try:
            self.fetch_history_data_from_db()
        except:
            logger.error(traceback.format_exc())
        '''

        await self.ex1.fetch_balance()
        await self.ex2.fetch_balance()

        self.init_spread_amount()

        # 数据不足，不计算, 等待足够的数据
        while len(self.spread1List) < self.sma_window_size or len(self.spread2List) < self.sma_window_size:
            if self.fetch_order_book():
                await asyncio.sleep(0.5)
            else:
                await asyncio.sleep(0.3)
            #logger.debug(self.to_string() + ';data len=' + str(len(self.spread1List)))
            

    # 计算移动平均
    def calc_sma_and_deviation(self):
        self.spread1_mean = numpy.mean(self.spread1List[-1 * self.sma_window_size:])
        self.spread1_stdev = numpy.std(self.spread1List[-1 * self.sma_window_size:])
        self.spread2_mean = numpy.mean(self.spread2List[-1 * self.sma_window_size:])
        self.spread2_stdev = numpy.std(self.spread2List[-1 * self.sma_window_size:])

    def init_spread_amount(self):
        if self.spread1_pos_amount > 0:
            self.current_position_direction = 1
        elif self.spread2_pos_amount > 0:
            self.current_position_direction = 2
        else:
            self.current_position_direction = 0

    # 仓位 再平衡
    def rebalance_set(self, rebalance_on, proportion):
        #logger.debug(self.to_string() + 'rebalance_set({0}, {1})'.format(rebalance_on, proportion))
        self.rebalance_on = rebalance_on
        self.ex1.rebalanced_position_proportion = proportion
        self.ex2.rebalanced_position_proportion = proportion

    async def rebalance_position(self):
        if not self.rebalance_on:
            logger.debug(self.to_string() + 'rebalance_position() return rebalance_on')
            return
        if self.ex1.rebalanced_position_proportion <= 0.0 or self.ex2.rebalanced_position_proportion <= 0.0:
            logger.debug(self.to_string() + 'rebalance_position() return proportion <= 0.0')
            return
        await self.ex1.rebalance_position(self.symbol)
        await self.ex2.rebalance_position(self.symbol)
        # 1: long spread1(buy okcoin, sell huobi);  
        # 2: long spread2( buy huobi, sell okcoin), 
        # 0: no position
        self.current_position_direction = 0
        self.spread1_pos_amount = 0
        self.spread2_pos_amount = 0
        #logger.debug(self.to_string() + 'rebalance_position() end')

    # 判断开仓、平仓
    def calc_position_direction(self):
        #logger.debug(self.to_string() + 'calc_position_direction() start')
        diff_spread1 = (self.spread1List[-1] - self.spread1_mean)
        diff_spread2 = (self.spread2List[-1] - self.spread2_mean)
        slippage_sum = self.ex1.slippage_value + self.ex2.slippage_value
        fee_sum_1 = (self.ex1.fee_taker  + self.ex2.fee_taker) * 4 * self.ex1.buy_1_price
        fee_sum_2 = (self.ex1.fee_taker  + self.ex2.fee_taker) * 4 * self.ex2.buy_1_price
        # 没有仓位
        if self.current_position_direction == 0:
            if diff_spread1 > self.spread1_stdev * self.spread1_open_condition_stdev_coe and diff_spread1 > slippage_sum * 1.5 + fee_sum_1:
                return 1
            elif diff_spread2 > self.spread2_stdev * self.spread2_open_condition_stdev_coe and diff_spread2 > slippage_sum * 1.5 + fee_sum_2:
                return 2
        # 已有仓位, 方向1 (sell exchange1, buy exchange2)
        elif self.current_position_direction == 1:
            # 还是方向1，可以继续判断，是否加仓
            if diff_spread1 > self.spread1_stdev * self.spread1_open_condition_stdev_coe and diff_spread1 > slippage_sum * 1.5 + fee_sum_1:
                return 1
            # 平仓
            if diff_spread2 > self.spread2_stdev * -self.spread1_close_condition_stdev_coe:
                return 2
        # 已有仓位, 方向2 (buy exchange1, sell exchange2)
        elif self.current_position_direction == 2:
            # 还是方向2，可以继续判断，是否加仓
            if diff_spread2 > self.spread2_stdev * self.spread2_open_condition_stdev_coe and diff_spread2 > slippage_sum * 1.5 + fee_sum_2:
                return 2
            # 平仓
            if diff_spread1 > self.spread1_stdev * -self.spread2_close_condition_stdev_coe:
                return 1
        # 没有达到搬砖条件
        logger.debug(self.to_string() + 'calc_position_direction() end  没有达到搬砖条件')
        return 0

    async def run_arbitrage(self):
        await self.init_data()
        a1 = await self.ex1.balance_amount_min(self.symbol)
        a2 = await self.ex2.balance_amount_min(self.symbol)
        self.amount_min = max(a1, a2)
        logger.debug(self.to_string() + "run_arbitrage() amount_min={0}".format(self.amount_min))
        while True:
            # 取订单深度信息
            if not self.fetch_order_book():
                await asyncio.sleep(0.3)
                continue

            # 时间太久，跳过
            cur_t = int(time.time())
            timeout_warn = 10
            if self.ex1.order_book_time < cur_t - timeout_warn or self.ex2.order_book_time < cur_t - timeout_warn:
                logger.debug(self.to_string() + "run_arbitrage() 时间太久，跳过")
                continue

            # 计算方差
            self.calc_sma_and_deviation()

            # 价格回归平均值，平衡仓位
            if self.rebalance_on:
                c1 = (self.spread1List[-1] - self.spread1_mean) 
                c2 = (self.spread2List[-1] - self.spread2_mean) 
                if c1 < self.spread1_stdev * self.spread1_close_condition_stdev_coe or c2 < self.spread2_stdev * self.spread2_close_condition_stdev_coe:
                    logger.debug(self.to_string() + "run_arbitrage() 平衡仓位 {0}, {1}".format(c1, c2))
                    await self.rebalance_position()
                    continue

            # 检查是否有机会
            amount_todo = 0.0
            position_direction = self.calc_position_direction()
            if position_direction == 0:
                # 没有交易信号，继续=
                #logger.debug(self.to_string() + "run_arbitrage() 没有交易信号，继续=")
                continue
            elif position_direction == 1:
                #logger.debug(self.to_string() + "run_arbitrage() position_direction == 1")
                self.log(position_direction)
                if self.current_position_direction == 0:  # 当前没有持仓
                    # 计算第1次开仓数量
                    amount_todo = min(self.ex1.buy_1_quantity, self.ex2.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 1:  # 当前long spread1
                    # 已有仓位，计算加仓数量
                    amount_todo = min(self.ex1.buy_1_quantity, self.ex2.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 2:  # 当前long spread2
                    # 另一个方向有仓位，计算可以减仓的数量
                    depth_amount = min(self.ex1.buy_1_quantity, self.ex2.sell_1_quantity) * self.order_book_ratio
                    amount_todo = min(depth_amount, self.spread2_pos_amount)

                # 以最小单位下注
                amount_todo = min(amount_todo, self.amount_min)

                # 账户中的钱，最大可以开多少仓位
                can_op_amount_1 = self.ex1.balance[self.base]['free']
                can_op_amount_2 = self.ex2.balance[self.quote]['free'] / self.ex2.sell_1_price * 0.98
                amount_max = min(can_op_amount_1, can_op_amount_2)
                amount_todo = min(amount_todo, amount_max)

                # 计算出的交易量 < 交易所要求的最小量 : 无法下单，忽略这次机会
                if amount_todo <= self.amount_min:
                    logger.debug(self.to_string() + "run_arbitrage() 计算出的交易量 < 交易所要求的最小量 : 无法下单，忽略这次机会")
                    continue
                await self.do_order_spread1(amount_todo)
                await self.ex1.fetch_balance()
                await self.ex2.fetch_balance()

            elif position_direction == 2:
                #logger.debug(self.to_string() + "run_arbitrage() position_direction == 2")
                self.log(position_direction)
                if self.current_position_direction == 0:  # 当前没有持仓
                    # 计算第1次开仓数量
                    amount_todo = min(self.ex2.buy_1_quantity, self.ex1.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 2:  # 当前long spread2
                    # 已有仓位，计算加仓数量
                    amount_todo = min(self.ex2.buy_1_quantity, self.ex1.sell_1_quantity) * self.order_book_ratio
                elif self.current_position_direction == 1:  # 当前long spread1
                    # 另一个方向有仓位，计算可以减仓的数量
                    depth_amount = min(self.ex2.buy_1_quantity, self.ex1.sell_1_quantity) * self.order_book_ratio
                    amount_todo = min(depth_amount, self.spread1_pos_amount)

                # 以最小单位下注
                amount_todo = min(amount_todo, self.amount_min)

                # 账户中的钱，最大可以开多少仓位
                can_op_amount_1 = self.ex1.balance[self.quote]['free'] / self.ex1.sell_1_price * 0.98
                can_op_amount_2 = self.ex2.balance[self.base]['free']
                amount_max = min(can_op_amount_1, can_op_amount_2)
                amount_todo = min(amount_todo, amount_max)
                # 计算出的交易量 < 交易所要求的最小量 : 无法下单，忽略这次机会
                if amount_todo <= self.amount_min:
                    logger.debug(self.to_string() + "run_arbitrage() 计算出的交易量 < 交易所要求的最小量 : 无法下单，忽略这次机会")
                    continue
                await self.do_order_spread2(amount_todo)
                await self.ex1.fetch_balance()
                await self.ex2.fetch_balance()

            if self.spread1_pos_amount > 0:
                self.current_position_direction = 1
            elif self.spread2_pos_amount > 0:
                self.current_position_direction = 2
            else:
                self.current_position_direction = 0
            
            # 完成一次套利，sleep 一下
            time.sleep(15)
            




    def log(self, position_direction):
        diff_spread1 = (self.spread1List[-1] - self.spread1_mean)
        diff_spread2 = (self.spread2List[-1] - self.spread2_mean)
        slippage_sum = self.ex1.slippage_value + self.ex2.slippage_value
        fee_sum_1 = (self.ex1.fee_taker  + self.ex2.fee_taker) * 4 * self.ex1.buy_1_price
        fee_sum_2 = (self.ex1.fee_taker  + self.ex2.fee_taker) * 4 * self.ex2.buy_1_price
        s = '\n' + self.symbol + ';position_direction=' + str(position_direction) + '\n'     \
            + 'ex1=' + self.ex1.ex.id + ',bid=' + str(self.ex1.buy_1_price) + ',ask=' + str(self.ex1.sell_1_price) + '\n'    \
            + 'ex2=' + self.ex2.ex.id + ',bid=' + str(self.ex2.buy_1_price) + ',ask=' + str(self.ex2.sell_1_price) + '\n'    \
            + "spread1List=" + str(self.spread1List[-1]) + ",spread1_mean=" + str(self.spread1_mean) + ",spread1_stdev=" + str(self.spread1_stdev) + ',diff_spread1=' + str(diff_spread1 / self.spread1_stdev) + '\n' \
            + "spread2List=" + str(self.spread2List[-1]) + ",spread2_mean=" + str(self.spread2_mean) + ",spread2_stdev=" + str(self.spread2_stdev) + ',diff_spread2=' + str(diff_spread2 / self.spread2_stdev) + '\n' \
            + "slippage_sum=" + str(self.ex1.slippage_value + self.ex2.slippage_value) + '\n'   \
            + "fee_sum_1=" + str((self.ex1.fee_taker  + self.ex2.fee_taker) * 4 * self.ex1.buy_1_price) + '\n'  \
            + "fee_sum_2=" + str((self.ex1.fee_taker  + self.ex2.fee_taker) * 4 * self.ex2.buy_1_price) + '\n'  \
            + 'diff_spread1=' + str(diff_spread1) + ',fee_total_1=' + str(slippage_sum * 1.5 + fee_sum_1) + '\n'    \
            + 'diff_spread2=' + str(diff_spread2) + ',fee_total_2=' + str(slippage_sum * 1.5 + fee_sum_2) + '\n'
        if position_direction == 1:
            s = s + self.symbol + ';sell=' + str(self.ex1.buy_1_price) + ';buy=' + str(self.ex2.sell_1_price) + '\n'
        if position_direction == 2:
            s = s + self.symbol + ';buy=' + str(self.ex1.sell_1_price) + ';sell=' + str(self.ex2.buy_1_price)
        logger.info(s)

    # 先执行第1个交易所的下单，等交易结果
    async def do_order_spread1(self, amount):
        ret = await self.ex1.sell_cancel(self.ex1.symbol, amount)
        if ret['filled'] is None or ret['filled'] <= 0:    # 订单完全没有成交，等待下一次机会
            return
        # 第1交易所已下单成功，第2交易所下单
        await self.ex2.buy_all(self.ex2.symbol, ret['filled'])
        if self.current_position_direction == 0 or self.current_position_direction == 1:
            self.spread1_pos_amount += ret['filled']
        elif self.current_position_direction == 2:
            self.spread2_pos_amount -= ret['filled']

    async def do_order_spread2(self, amount):
        ret = await self.ex2.sell_cancel(self.ex2.symbol, amount)
        if ret['filled'] is None or ret['filled'] <= 0:    # 订单完全没有成交，等待下一次机会
            return
        # 第2交易所已下单成功，第1交易所下单
        await self.ex1.buy_all(self.ex1.symbol, ret['filled'])
        if self.current_position_direction == 0 or self.current_position_direction == 2:
            self.spread2_pos_amount += ret['filled']
        elif self.current_position_direction == 1:
            self.spread1_pos_amount -= ret['filled']

                




    async def run(self, func, *args, **kwargs):
        while True:
            try:
                await func(*args, **kwargs)
            except ccxt.RequestTimeout:
                logger.info(self.to_string() + "run() ccxt.RequestTimeout ")
                await asyncio.sleep(3)
            except ccxt.DDoSProtection:
                logger.info(self.to_string() + "run() ccxt.DDoSProtection ")
                await asyncio.sleep(3)
            except ccxt.AuthenticationError:
                logger.info(self.to_string() + "run() ccxt.AuthenticationError ")
                await asyncio.sleep(3)
            except ccxt.ExchangeNotAvailable:
                logger.info(self.to_string() + "run() ccxt.ExchangeNotAvailable ")
                await asyncio.sleep(3)
            except ccxt.ExchangeError:
                logger.info(self.to_string() + "run() ccxt.ExchangeError ")
                await asyncio.sleep(3)
            except ccxt.NetworkError:
                logger.info(self.to_string() + "run() ccxt.NetworkError ")
                await asyncio.sleep(3)
            except Exception as e:
                logger.info(self.to_string() + "run() {}".format(e))
                await asyncio.sleep(3)
            except:
                logger.error(traceback.format_exc())
