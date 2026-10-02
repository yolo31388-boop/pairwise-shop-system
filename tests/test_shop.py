"""商店商城 - 红态测试"""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shop.system import ShopSystem, ShopItem

class TestAtomicPurchase:
    def test_no_charge_when_out_of_stock(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 0)
        player = {"gold": 200}
        result = ss.purchase_item("i1", player)
        assert player["gold"] == 200

class TestStockConcurrency:
    def test_stock_not_oversold(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 1)
        player = {"gold": 1000}
        ss.purchase_item("i1", player)
        ss.purchase_item("i1", player)
        assert ss.items["i1"].stock == 0

class TestDiscountPrice:
    def test_vip_gets_discount(self):
        ss = ShopSystem()
        item = ShopItem("i1", "sword", 100, 10)
        player = {"vip": True}
        price = ss.calculate_price(item, player)
        assert price < 100

class TestAccountLimit:
    def test_limit_checks_account(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 1, limit=1)
        player = {"account_id": "acc1", "today_purchases": {"i1": 1}}
        assert ss.check_purchase_limit("i1", player) == False

class TestRefreshPreservesLog:
    def test_refresh_preserves_purchase_history(self):
        ss = ShopSystem()
        ss.items["i1"] = ShopItem("i1", "sword", 100, 10)
        ss.purchase_log.append({"iid": "i1", "time": 100})
        ss.refresh_shop()
        assert len(ss.purchase_log) == 1
