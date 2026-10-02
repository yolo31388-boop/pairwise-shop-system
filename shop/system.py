"""商店商城 - 含5个bug"""
from dataclasses import dataclass, field

@dataclass
class ShopItem:
    iid: str
    name: str
    price: float
    stock: int
    limit: int = 0  # 0=无限

class ShopSystem:
    def __init__(self):
        self.items: dict[str, ShopItem] = {}
        self.purchase_log: list = []  # bug2: 不记录销量

    def purchase_item(self, iid: str, player: dict) -> bool:
        # bug1: 先扣钱再检查库存，非原子
        item = self.items.get(iid)
        if not item:
            return False
        player["gold"] -= item.price
        if item.stock <= 0:
            return False
        item.stock -= 1
        return True

    def update_stock(self, iid: str, amount: int) -> None:
        # bug2: 不做并发控制
        item = self.items.get(iid)
        if item:
            item.stock += amount

    def calculate_price(self, item: ShopItem, player: dict) -> float:
        # bug3: 不考虑折扣VIP
        return item.price

    def check_purchase_limit(self, iid: str, player: dict) -> bool:
        # bug4: 只看今日，不看账号
        item = self.items.get(iid)
        if not item or item.limit == 0:
            return True
        return player.get("today_purchases", {}).get(iid, 0) < item.limit

    def refresh_shop(self) -> None:
        # bug5: 不保留购买记录
        for item in self.items.values():
            item.stock = 100
