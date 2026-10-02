"""商店商城"""
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class ShopItem:
    iid: str
    name: str
    price: float
    stock: int
    limit: int = 0  # 0=无限
    sold: int = 0   # 已售数量（用于统计销量）


@dataclass
class Discount:
    rate: float           # 0.8 表示 8 折
    expire_at: float = 0  # 过期时间戳，0 表示永久有效


class ShopSystem:
    VIP_DISCOUNT = 0.8          # VIP 8 折
    REFRESH_INTERVAL = 3600     # 固定刷新间隔（秒）
    RESTOCK_AMOUNT = 100

    def __init__(self, now=None):
        self.items: dict[str, ShopItem] = {}
        self.purchase_log: list = []
        self.discounts: dict[str, Discount] = {}
        # 账号维度的历史总购买记录：account_id -> {iid: count}
        self.account_purchases: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()
        self._now = now or time.time
        # 固定刷新时间点：对齐到固定间隔边界，玩家无法卡时间
        self.next_refresh_at = self._align_refresh_time(self._now())

    def _align_refresh_time(self, t: float) -> float:
        return (int(t // self.REFRESH_INTERVAL) + 1) * self.REFRESH_INTERVAL

    # ---- 购买 ----
    def purchase_item(self, iid: str, player: dict) -> bool:
        # 原子操作：扣钱、扣库存、发物品、记限购在同一把锁内完成
        with self._lock:
            item = self.items.get(iid)
            if not item:
                return False
            if item.stock <= 0:  # 先检查库存，再扣钱
                return False
            if not self.check_purchase_limit(iid, player):
                return False
            bag = player.get("bag")
            if bag is not None:
                capacity = player.get("bag_capacity", 0)
                if len(bag) >= capacity:  # 背包已满，不发货不扣钱
                    return False
            price = self.calculate_price(item, player)
            if player.get("gold", 0) < price:
                return False

            player["gold"] -= price
            item.stock -= 1
            item.sold += 1
            if bag is not None:
                bag.append(iid)
            account_id = player.get("account_id")
            if account_id is not None:
                acc = self.account_purchases.setdefault(account_id, {})
                acc[iid] = acc.get(iid, 0) + 1
            today = player.setdefault("today_purchases", {})
            today[iid] = today.get(iid, 0) + 1
            self.purchase_log.append({
                "iid": iid,
                "account_id": account_id,
                "price": price,
                "time": self._now(),
            })
            return True

    # ---- 库存 ----
    def update_stock(self, iid: str, amount: int) -> None:
        with self._lock:  # 并发控制
            item = self.items.get(iid)
            if item:
                item.stock += amount
                if amount < 0:
                    item.sold += -amount  # 记录销量

    def get_sold(self, iid: str) -> int:
        item = self.items.get(iid)
        return item.sold if item else 0

    # ---- 价格 ----
    def calculate_price(self, item: ShopItem, player: dict) -> float:
        price = Decimal(str(item.price))
        now = self._now()
        discount = self.discounts.get(item.iid)
        if discount and (discount.expire_at == 0 or now < discount.expire_at):
            price *= Decimal(str(discount.rate))  # 过期折扣不生效
        if player.get("vip"):
            price *= Decimal(str(self.VIP_DISCOUNT))
        # 正确四舍五入到分，99.5 不会被错误地进位成 100
        return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    # ---- 限购 ----
    def check_purchase_limit(self, iid: str, player: dict) -> bool:
        with self._lock:
            item = self.items.get(iid)
            if not item or item.limit == 0:
                return True
            account_id = player.get("account_id")
            account_total = 0
            if account_id is not None:
                account_total = self.account_purchases.get(account_id, {}).get(iid, 0)
            today = player.get("today_purchases", {}).get(iid, 0)
            # 账号维度历史总限购（取账号记录与当日记录的最大值，避免重复计数）
            return max(account_total, today) < item.limit

    # ---- 刷新 ----
    def refresh_shop(self) -> None:
        with self._lock:
            for item in self.items.values():
                item.stock = self.RESTOCK_AMOUNT
            # 保留 purchase_log 与 account_purchases，限购不因刷新重置
            self.next_refresh_at = self._align_refresh_time(self._now())

    def maybe_refresh(self) -> bool:
        """到达固定刷新时间点才刷新，返回是否发生了刷新。"""
        with self._lock:
            if self._now() >= self.next_refresh_at:
                self.refresh_shop()
                return True
            return False
