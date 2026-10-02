"""商店商城 - 红态测试（14 个）"""
import threading

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shop.system import ShopSystem, ShopItem, Discount


class TestAtomicPurchase:
    def test_no_charge_when_out_of_stock(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 0)
        player = {"gold": 200}
        result = ss.purchase_item("i1", player)
        assert result is False
        assert player["gold"] == 200

    def test_purchase_is_atomic(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 5)
        player = {"gold": 250, "bag": []}
        assert ss.purchase_item("i1", player) is True
        assert player["gold"] == 150
        assert player["bag"] == ["i1"]
        assert ss.items["i1"].stock == 4
        assert ss.items["i1"].sold == 1

    def test_no_charge_when_bag_full(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 5)
        player = {"gold": 500, "bag": ["x", "y"], "bag_capacity": 2}
        assert ss.purchase_item("i1", player) is False
        assert player["gold"] == 500
        assert ss.items["i1"].stock == 5


class TestStockConcurrency:
    def test_stock_not_oversold(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 1)
        player = {"gold": 1000}
        ss.purchase_item("i1", player)
        ss.purchase_item("i1", player)
        assert ss.items["i1"].stock == 0
        assert player["gold"] == 900

    def test_concurrent_last_item_only_one_wins(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 1)
        players = [{"gold": 1000, "bag": []} for _ in range(10)]
        results = []

        def buy(p):
            results.append(ss.purchase_item("i1", p))

        threads = [threading.Thread(target=buy, args=(p,)) for p in players]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert ss.items["i1"].stock == 0
        assert ss.items["i1"].sold == 1

    def test_sales_recorded(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 10)
        player = {"gold": 1000, "bag": []}
        ss.purchase_item("i1", player)
        ss.purchase_item("i1", player)
        assert ss.items["i1"].sold == 2
        assert len(ss.purchase_log) == 2


class TestDiscountPrice:
    def test_vip_gets_discount(self):
        ss = ShopSystem()
        item = ShopItem("i1", "sword", 100, 10)
        player = {"vip": True}
        price = ss.calculate_price(item, player)
        assert price < 100

    def test_expired_discount_ignored(self):
        ss = ShopSystem()
        item = ShopItem("i1", "sword", 100, 10,
                        discount=Discount(rate=0.5, start=0, end=100))
        assert ss.calculate_price(item, {}, now=50) == 50.0
        assert ss.calculate_price(item, {}, now=200) == 100.0

    def test_price_rounding_half_up(self):
        ss = ShopSystem()
        item = ShopItem("i1", "gem", 0.995, 10)
        assert ss.calculate_price(item, {}) == 1.00
        item2 = ShopItem("i2", "gem2", 99.5, 10)
        assert ss.calculate_price(item2, {}) == 99.5


class TestAccountLimit:
    def test_limit_checks_account(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 1, limit=1)
        player = {"account_id": "acc1", "today_purchases": {"i1": 1}}
        assert ss.check_purchase_limit("i1", player) == False

    def test_limit_shared_across_characters(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 10, limit=1)
        char1 = {"account_id": "acc1", "char_id": "c1", "gold": 1000, "bag": []}
        char2 = {"account_id": "acc1", "char_id": "c2", "gold": 1000, "bag": []}
        assert ss.purchase_item("i1", char1) is True
        assert ss.check_purchase_limit("i1", char2) is False
        assert ss.purchase_item("i1", char2) is False
        assert ss.items["i1"].sold == 1

    def test_limit_atomic_under_concurrency(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 100, limit=2)
        player = {"account_id": "acc1", "gold": 100000, "bag": []}
        results = []

        def buy():
            results.append(ss.purchase_item("i1", player))

        threads = [threading.Thread(target=buy) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 2
        assert ss.items["i1"].sold == 2


class TestRefresh:
    def test_refresh_preserves_log_and_limits(self):
        ss = ShopSystem(refresh_interval=100)
        ss.items["i1"] = ShopItem("i1", "sword", 100, 10, limit=1)
        player = {"account_id": "acc1", "gold": 1000, "bag": []}
        ss.purchase_item("i1", player, now=50)
        ss.purchase_log.append({"iid": "i1", "time": 100})
        ss.refresh_shop(now=50)   # 首次调用只建立时间表
        ss.refresh_shop(now=150)  # 到达固定刷新点
        assert len(ss.purchase_log) == 2
        assert ss.items["i1"].stock == 100
        # 限购不随刷新重置
        assert ss.check_purchase_limit("i1", player) is False
        assert ss.purchase_item("i1", player, now=150) is False

    def test_refresh_only_at_fixed_time(self):
        ss = ShopSystem(refresh_interval=100)
        ss.items["i1"] = ShopItem("i1", "sword", 100, 3)
        ss.refresh_shop(now=50)          # t=100 建立时间表
        assert ss.refresh_shop(now=99) is False
        assert ss.items["i1"].stock == 3
        assert ss.refresh_shop(now=100) is True
        assert ss.items["i1"].stock == 100
        # 刷新点之间重复调用不生效，无法卡点刷货
        assert ss.refresh_shop(now=150) is False
        assert ss.refresh_shop(now=200) is True
