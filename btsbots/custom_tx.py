# -*- coding: utf-8 -*-
from collections import OrderedDict

from graphenebase.types import (
    Optional,
    PointInTime,
    Set,
    Array,
)
from bitsharesbase.objects import (
    Asset,
    Price,
    ObjectId,
    GrapheneObject,
    isArgsThisClass,
)
import bitsharesbase.operations as bts_ops
from bitsharesbase.signedtransactions import Signed_Transaction
import bitsharesbase.operationids as bts_opids

# ==========================================
# 1. 严格按照 C++ 结构定义 77 号操作
# ==========================================
class Limit_order_update(GrapheneObject):
    """
    BitShares limit_order_update_operation (opcode: 77)
    严格遵循 C++ struct limit_order_update_operation 字段排队：
    1. fee (asset)
    2. seller (account_id_type)
    3. order (limit_order_id_type)
    4. new_price (optional<price>)
    5. delta_amount_to_sell (optional<asset>)
    6. new_expiration (optional<time_point_sec>)
    7. on_fill (optional<vector<limit_order_auto_action>>)
    8. extensions (extensions_type)
    """
    def __init__(self, *args, **kwargs):
        if isArgsThisClass(self, args):
            self.data = args[0].data
        else:
            if len(args) == 1 and len(kwargs) == 0:
                kwargs = args[0]
            prefix = kwargs.get("prefix", "BTS")

            # 1. new_price: optional<price>
            if "new_price" in kwargs and kwargs["new_price"]:
                new_price = Optional(Price(kwargs["new_price"]))
            else:
                new_price = Optional(None)

            # 2. delta_amount_to_sell: optional<asset>
            if "delta_amount_to_sell" in kwargs and kwargs["delta_amount_to_sell"]:
                delta_amount_to_sell = Optional(Asset(kwargs["delta_amount_to_sell"]))
            else:
                delta_amount_to_sell = Optional(None)

            # 3. new_expiration: optional<time_point_sec>
            if "new_expiration" in kwargs and kwargs["new_expiration"]:
                new_expiration = Optional(PointInTime(kwargs["new_expiration"]))
            else:
                new_expiration = Optional(None)

            # 4. on_fill: optional<vector<limit_order_auto_action>>
            if "on_fill" in kwargs and kwargs["on_fill"]:
                on_fill = Optional(Array(kwargs["on_fill"]))
            else:
                on_fill = Optional(None)

            super().__init__(
                OrderedDict(
                    [
                        ("fee", Asset(kwargs["fee"])),
                        ("seller", ObjectId(kwargs["seller"], "account")),
                        ("order", ObjectId(kwargs["order"], "limit_order")),
                        ("new_price", new_price),
                        ("delta_amount_to_sell", delta_amount_to_sell),
                        ("new_expiration", new_expiration),
                        ("on_fill", on_fill),
                        ("extensions", Set([])),
                    ]
                )
            )

# ==========================================
# 2. 注入 77 号操作到 bitsharesbase 全局映射表
# ==========================================
bts_opids.operations["limit_order_update"] = 77
setattr(bts_ops, "Limit_order_update", Limit_order_update)
bts_ops.class_idmap[77] = Limit_order_update
bts_ops.class_namemap["Limit_order_update"] = 77
bts_ops.fill_classmaps()

# ==========================================
# 3. 自定义 Signed_Transaction 类
# ==========================================
class New_Signed_Transaction(Signed_Transaction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)