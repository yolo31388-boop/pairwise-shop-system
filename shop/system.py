"""商店商城系统。

修复点：
- purchase_item: 先校验（商品/限购/库存/背包/金钱），再在同一把锁内
  原子地完成 扣钱 -> 扣库存 -> 发物品 -> 记销量/限购，失败回滚，任何
  中途异常都不会导致钱货两空。
- update_stock: 加锁串行化，库存绝不允许为负；销量单独记录（sold）。
- calculate_price: 支持有效期内的折扣 + VIP 折扣，使用 ROUND_HALF_UP
  正确四舍五入（0.995 -> 1.00、99.5 -> 100）。
- check_purchase_limit: 按账号（account_id）维度统计历史总限购，
  与购买共用同一把锁，检查与计数更新原子完成。
- refresh_shop: 固定刷新周期（整点对齐），未到刷新点不补货；
  刷新只补库存，保留 purchase_log 与账号限购计数，限购不重置。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

DEFAULT_STOCK = 100
DEFAULT_REFRESH_INTERVAL = 3600
DEFAULT_VIP_DISCOUNT = Decimal("0.9")
PRICE_QUANT = Decimal("0.01")


def round_price(value) -> float:
    """银行/商城通用的四舍五入（ROUND_HALF_UP），保留两位小数。"""
    return float(
        Decimal(str(value)).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)
    )


@dataclass
class Discount:
    """折扣券/活动折扣。rate 为实际支付比例，如 0.8 表示八折。"""

    rate: float
    start: float = 0.0
    end: float = float("inf")
    name: str = ""

    def is_valid(self, now: float) -> bool:
        return self.start <= now <= self.end

    def factor(self, now: float) -> Decimal:
        return Decimal(str(self.rate)) if self.is_valid(now) else Decimal("1")


@dataclass
class ShopItem:
    iid: str
    name: str
    price: float
    stock: int = DEFAULT_STOCK
    limit: int = 0  # 0 = 不限购（账号维度历史总量）
    sold: int = 0
    discount: Discount | None = None


class ShopSystem:
    def __init__(self, refresh_interval: int = DEFAULT_REFRESH_INTERVAL):
        self.items: dict[str, ShopItem] = {}
        self.purchase_log: list[dict] = []
        # 账号 -> {iid: 历史购买总数}，跨角色、跨刷新累计
        self.account_purchases: dict[str, dict[str, int]] = {}
        self.global_discounts: dict[str, Discount] = {}
        self.refresh_interval = refresh_interval
        self._next_refresh: float | None = None
        self._lock = threading.RLock()

    # ---------- 折扣 ----------

    def add_discount(self, iid: str, discount: Discount) -> None:
        """给指定商品挂折扣；iid 为 None/"*" 时为全场折扣。"""
        with self._lock:
            if iid is None or iid == "*":
                self.global_discounts[id(discount)] = discount
            else:
                item = self.items.get(iid)
                if item is not None:
                    item.discount = discount

    def calculate_price(
        self,
        item: ShopItem,
        player: dict | None = None,
        now: float | None = None,
    ) -> float:
        """计算玩家实际支付价：标价 × 有效折扣 × VIP 折扣，四舍五入到分。"""
        player = player or {}
        if now is None:
            now = time.time()

        price = Decimal(str(item.price))

        discount = item.discount
        if discount is not None:
            price *= discount.factor(now)
        for global_discount in self.global_discounts.values():
            price *= global_discount.factor(now)

        if player.get("vip"):
            price *= DEFAULT_VIP_DISCOUNT

        return round_price(price)

    # ---------- 限购 ----------

    @staticmethod
    def _account_id(player: dict) -> str:
        return str(
            player.get("account_id")
            or player.get("account")
            or player.get("pid")
            or id(player)
        )

    def get_purchase_count(self, iid: str, player: dict) -> int:
        """账号维度的历史购买总量（兼容旧的 today_purchases 计数）。"""
        account = self._account_id(player)
        account_total = self.account_purchases.get(account, {}).get(iid, 0)
        legacy_today = player.get("today_purchases", {}).get(iid, 0)
        return max(account_total, legacy_today)

    def check_purchase_limit(self, iid: str, player: dict) -> bool:
        """返回该账号当前是否还能买 iid（历史总限购，跨角色生效）。"""
        item = self.items.get(iid)
        if not item or item.limit == 0:
            return True
        return self.get_purchase_count(iid, player) < item.limit

    # ---------- 背包 / 金钱 ----------

    @staticmethod
    def _bag_free_space(player: dict) -> int:
        if "bag" in player and isinstance(player["bag"], (list, dict)):
            capacity = player.get(
                "bag_capacity",
                player.get("bag_max", player.get("bag_limit", 10**9)),
            )
            return max(0, int(capacity) - len(player["bag"]))
        if "inventory" in player and isinstance(player["inventory"], list):
            capacity = player.get("bag_capacity", player.get("bag_max", 10**9))
            return max(0, int(capacity) - len(player["inventory"]))
        # 没有背包概念的玩家视为空间无限
        return 10**9

    @staticmethod
    def _put_into_bag(player: dict, iid: str) -> None:
        if "bag" in player and isinstance(player["bag"], list):
            player["bag"].append(iid)
        elif "inventory" in player and isinstance(player["inventory"], list):
            player["inventory"].append(iid)
        elif "bag" in player and isinstance(player["bag"], dict):
            player["bag"][iid] = player["bag"].get(iid, 0) + 1
        else:
            player.setdefault("bag", []).append(iid)

    # ---------- 库存 ----------

    def update_stock(self, iid: str, amount: int) -> bool:
        """加锁调整库存（补货为正、扣减为负），库存不允许为负。"""
        with self._lock:
            item = self.items.get(iid)
            if item is None:
                return False
            if item.stock + amount < 0:
                return False
            item.stock += amount
            return True

    # ---------- 购买 ----------

    def purchase_item(
        self,
        iid: str,
        player: dict,
        qty: int = 1,
        now: float | None = None,
    ) -> bool:
        """原子购买：全部前置校验通过后，在单把锁内完成钱货交换。"""
        if qty <= 0:
            return False
        if now is None:
            now = time.time()

        with self._lock:
            item = self.items.get(iid)
            if item is None:
                return False

            # 限购（账号维度历史总量）
            if item.limit != 0:
                already = self.get_purchase_count(iid, player)
                if already + qty > item.limit:
                    return False

            # 库存
            if item.stock < qty:
                return False

            # 背包空间
            if self._bag_free_space(player) < qty:
                return False

            # 价格与金钱（在锁内计算，保证用的是同一时刻的折扣）
            unit_price = self.calculate_price(item, player, now)
            total_price = round_price(Decimal(str(unit_price)) * qty)
            gold = player.get("gold", 0)
            if gold < total_price:
                return False

            # ---- 以下为提交阶段，整体在锁内，异常时回滚 ----
            old_gold = gold
            added: list[str] = []
            try:
                player["gold"] = gold - total_price
                for _ in range(qty):
                    self._put_into_bag(player, iid)
                    added.append(iid)
                item.stock -= qty
                item.sold += qty
                account = self._account_id(player)
                record = self.account_purchases.setdefault(account, {})
                record[iid] = record.get(iid, 0) + qty
                self.purchase_log.append(
                    {
                        "iid": iid,
                        "account_id": account,
                        "char_id": player.get("char_id")
                        or player.get("cid"),
                        "qty": qty,
                        "price": total_price,
                        "time": now,
                    }
                )
            except Exception:
                # 回滚：退钱、收回物品、恢复库存与销量
                player["gold"] = old_gold
                if "bag" in player and isinstance(player["bag"], list):
                    for iid_added in added:
                        player["bag"].remove(iid_added)
                elif "inventory" in player and isinstance(
                    player["inventory"], list
                ):
                    for iid_added in added:
                        player["inventory"].remove(iid_added)
                item.stock += len(added)
                item.sold -= len(added)
                raise

            return True

    # ---------- 刷新 ----------

    def _aligned_refresh_points(self, now: float) -> tuple[float, float]:
        """返回当前所处的固定刷新窗口 (窗口起点, 下一次刷新点)。"""
        interval = self.refresh_interval
        current_window = (int(now) // interval) * interval
        return float(current_window), float(current_window + interval)

    def refresh_shop(self, now: float | None = None) -> bool:
        """固定周期刷新：只在刷新点补货，购买记录与限购计数永不重置。"""
        if now is None:
            now = time.time()

        with self._lock:
            window_start, next_refresh = self._aligned_refresh_points(now)
            if self._next_refresh is None:
                # 首次调用只建立固定时间表，不补货
                self._next_refresh = next_refresh
                return False
            if now < self._next_refresh:
                # 未到固定刷新点，任何调用都不生效，无法卡点刷货
                return False

            for item in self.items.values():
                item.stock = DEFAULT_STOCK
            # 对齐到刷新网格，避免累计漂移
            self._next_refresh = (
                (int(now) // self.refresh_interval) + 1
            ) * self.refresh_interval
            return True
