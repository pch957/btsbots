from btsbots.bots_client import BotsClient

class BTSBots(BotsClient):
    async def run(self, passPath=""):
        await super().run()
        # 最新区块信息
        await self.subscribe("chainBlockHeadStream")
        # 手续费参数
        await self.subscribe("chainGlobalProperties")

    async def make_transaction(self, raw_ops: list[dict], isSim: bool=False) -> int:
        """
        接受交易请求数组，填充手续费，作为一个完整的 transaction 提交签名广播
        """
        try:
            ops = []
            for raw_op in raw_ops:
                op = await self._build_op(self.bts_id, raw_op)
                ops.append(op)
            self._fill_ops_fee(ops)
            block = await self._sign_and_broadcast(ops, isSim)
            return block
        except Exception as e:
            print(f"🚨 [交易失败] {e}")
            raise

    def _get_ref_block_info(self) -> tuple[int, int]:
        import struct
        from binascii import unhexlify
        block_head_coll = self.collections.get("global_properties")
        if not block_head_coll:
            raise KeyError("本地内存中暂未接收到同步的高度状态数据。")

        block_doc = next(iter(block_head_coll.values()))
        head_block_num = int(block_doc.get("B", 0))
        head_block_id_hex = str(block_doc.get("id", "")).strip()

        ref_block_num = head_block_num & 0xFFFF
        id_bytes = unhexlify(head_block_id_hex)
        ref_block_prefix = struct.unpack_from("<I", id_bytes, 4)[0]
        return ref_block_num, ref_block_prefix

    def _get_chain_time(self) -> int:
        block_head_coll = self.collections.get("global_properties", {})
        if not block_head_coll:
            raise KeyError("本地内存中暂未接收到同步的高度状态数据。")

        block_doc = next(iter(block_head_coll.values()))
        block_time = block_doc["T"]["$date"] / 1000
        return block_time

    def _fill_ops_fee(self, ops: list):
        global_coll = self.collections.get("global", {})
        if not global_coll:
            raise KeyError("本地内存中暂未接收到同步的费率数据。")
        global_doc = next((doc for doc in global_coll.values() if doc.get("id") == "2.0.0"))
        fee_doc = global_doc["parameters"].get("current_fees", {}).get("parameters", [])
        fee_map = {item[0]: item[1] for item in fee_doc if isinstance(item, list) and len(item) == 2}

        for op in ops:
            self._fill_op_fee(op, fee_map)

    def _fill_op_fee(self, op: list, fee_map: dict):
        from binascii import unhexlify
        op_code = op[0]
        if op_code not in fee_map:
            calculated_fee = 0
        else:
            item = fee_map[op_code]
            if op_code == 5:  # account_create
                calculated_fee = int(item.get("basic_fee", 0))
                total_bytes = 143 + len((op[1]["name"]).encode('utf-8'))
                calculated_fee += total_bytes * item.get("price_per_kbyte", 0) // 1024
            else:
                calculated_fee = int(item.get("fee", 0))

            if op_code == 0 and op[1].get("memo"):  # transfer memo
                cipher_bytes_len = len(unhexlify(op[1]["memo"]["message"]))
                varint_len = 2
                total_bytes = 33 + 33 + 8 + varint_len + cipher_bytes_len
                calculated_fee += total_bytes * item.get("price_per_kbyte", 0) // 1024

        op[1]["fee"]["amount"] = calculated_fee

    async def _sign_and_broadcast(self, ops: list, isSim: bool=False) -> int:
        from datetime import datetime
        try:
            ref_block_num, ref_block_prefix = self._get_ref_block_info()
            block_time = self._get_chain_time()
            dt = datetime.fromtimestamp(block_time + 30)
            expiration = dt.strftime("%Y-%m-%dT%H:%M:%S")
            payload = {
                "ref_block_num": int(ref_block_num),
                "ref_block_prefix": int(ref_block_prefix),
                "expiration": expiration,
                "operations": ops
            }

            active_pub = await self._resolve_account_active_pubkey(self.account_name)
            finalized_tx_json = self.key_manager.sign_transaction(active_pub, payload)

            if isSim:
                result = {"status": "SUCCESS", "blockNum": 1644}
            else:
                result = await self.call("broadcastTransaction", finalized_tx_json)
            if result.get("status") == "FAIL":
                print(f"  交易广播失败：{result.get('message')}")
                raise RuntimeError(f"  交易广播失败：{result.get('message')}")
            block_num = result["blockNum"]

            print(f"  发送成功，交易区块号: {block_num}")
            return block_num
        except Exception as broadcast_err:
            print(f"  [!] 交易被节点拒绝，错误明细: {broadcast_err}")
            raise

    async def _build_op(self, uid: str, raw_op: dict) -> list:
        op_type = raw_op.get("type")
        op_params = raw_op.get("params", {})
        _op = None

        if op_type == "transfer":
            _op = await self._build_op_transfer(uid, op_params)
        elif op_type == "limit_order_create":
            _op = await self._build_op_limit_order_create(uid, op_params)
        elif op_type == "limit_order_cancel":
            _op = await self._build_op_limit_order_cancel(uid, op_params)
        elif op_type == "limit_order_update":
            _op = await self._build_op_limit_order_update(uid, op_params)
        elif op_type == "account_create":
            _op = await self._build_op_account_create(uid, op_params)
        elif op_type == "withdraw_vesting":
            _op = await self._build_op_withdraw_vesting(uid, op_params)

        if _op is None:
            raise RuntimeError(f"未支持的交易类型: {op_type}")
        return _op

    async def _build_op_transfer(self, uid: str, op_params: dict) -> list:
        memo_payload = None
        if op_params.get("memo"):
            memo_payload = await self.encrypt_memo(op_params)

        _, to_id = await self.get_account_brief(op_params.get("to_account"))
        _, asset_id, asset_prec = await self.get_asset_brief(op_params.get("asset"))
        amount = int(float(op_params.get("amount")) * (10 ** asset_prec))
        transfer_payload = {
            "fee": {"amount": 0, "asset_id": "1.3.0"},
            "from": str(uid),
            "to": str(to_id),
            "amount": {
                "amount": amount,
                "asset_id": asset_id
            },
            "extensions": []
        }
        if memo_payload:
            transfer_payload["memo"] = memo_payload

        return [0, transfer_payload]

    async def _build_op_limit_order_create(self, uid: str, op_params: dict) -> list:
        from datetime import datetime, timedelta

        sell_symbol = str(op_params["sell_asset"]).upper().strip()
        recv_symbol = str(op_params["receive_asset"]).upper().strip()

        _, sell_id, sell_prec = await self.get_asset_brief(sell_symbol)
        _, recv_id, recv_prec = await self.get_asset_brief(recv_symbol)

        raw_sell_amount = int(float(op_params["amount"]) * (10 ** sell_prec))
        raw_recv_amount = int((float(op_params["amount"]) * float(op_params["price"])) * (10 ** recv_prec))

        block_time = self._get_chain_time()
        dt_base = datetime.fromtimestamp(block_time)
        expiration = (dt_base + timedelta(days=300)).strftime("%Y-%m-%dT%H:%M:%S")
        order_payload = {
            "fee": {"amount": 0, "asset_id": "1.3.0"},
            "seller": str(uid),
            "amount_to_sell": {
                "amount": raw_sell_amount,
                "asset_id": sell_id
            },
            "min_to_receive": {
                "amount": raw_recv_amount,
                "asset_id": recv_id
            },
            "expiration": expiration,
            "fill_or_kill": bool(op_params.get('fill_or_kill', False)),
            "extensions": []
        }
        return [1, order_payload]

    async def _build_op_limit_order_cancel(self, uid: str, op_params: dict) -> list:
        raw_order_id = str(op_params.get("order_id")).replace("~", "").strip()
        order_id = raw_order_id if raw_order_id.startswith("1.7.") else f"1.7.{raw_order_id}"

        cancel_payload = {
            "fee": {"amount": 0, "asset_id": "1.3.0"},
            "fee_paying_account": str(uid),
            "order": order_id,
            "extensions": []
        }
        return [2, cancel_payload]

    async def _build_op_limit_order_update(self, uid: str, op_params: dict) -> list:
        """
        BitShares limit_order_update_operation (opcode: 77)
        精准保证 new_price.base.amount <= max_amount_for_sale
        """
        raw_order_id = str(op_params.get("order_id")).replace("~", "").strip()
        order_id = raw_order_id if raw_order_id.startswith("1.7.") else f"1.7.{raw_order_id}"

        update_payload = {
            "fee": {"amount": 0, "asset_id": "1.3.0"},
            "seller": str(uid),
            "order": order_id,
            "new_price": None,
            "delta_amount_to_sell": None,
            "new_expiration": None,
            "on_fill": None,
            "extensions": []
        }

        sell_symbol = str(op_params["sell_asset"]).upper().strip()
        recv_symbol = str(op_params["receive_asset"]).upper().strip()
        _, sell_id, sell_prec = await self.get_asset_brief(sell_symbol)
        _, recv_id, recv_prec = await self.get_asset_brief(recv_symbol)

        # 1. 解析 delta_amount_to_sell
        raw_delta = 0
        if "delta_amount_to_sell" in op_params and op_params["delta_amount_to_sell"] is not None:
            delta_val = float(op_params["delta_amount_to_sell"])
            raw_delta = int(delta_val * (10 ** sell_prec))
            if raw_delta != 0:
                update_payload["delta_amount_to_sell"] = {
                    "amount": raw_delta,
                    "asset_id": sell_id
                }

        # 2. 解析 new_price
        if "price" in op_params and op_params["price"] is not None:
            # 基础卖出量：若传了 base_for_sale 则取其与 delta 的合计，确保 base.amount <= 链上实际剩余挂单量
            if "base_for_sale" in op_params and op_params["base_for_sale"] is not None:
                amount_base = float(op_params["base_for_sale"]) + (raw_delta / (10 ** sell_prec))
            else:
                amount_base = float(op_params.get("amount", 10.0))

            if amount_base <= 0:
                amount_base = float(op_params.get("amount", 1.0))

            raw_sell_amount = int(amount_base * (10 ** sell_prec))
            raw_recv_amount = int((amount_base * float(op_params["price"])) * (10 ** recv_prec))

            update_payload["new_price"] = {
                "base": {
                    "amount": raw_sell_amount,
                    "asset_id": sell_id
                },
                "quote": {
                    "amount": raw_recv_amount,
                    "asset_id": recv_id
                }
            }

        if "new_expiration" in op_params and op_params["new_expiration"]:
            update_payload["new_expiration"] = op_params["new_expiration"]

        return [77, update_payload]

    async def _build_op_account_create(self, uid: str, op_params: dict) -> list:
        account_create_payload = {
            "fee": {"amount": 0, "asset_id": "1.3.0"},
            "registrar": str(uid),
            "referrer": str(uid),
            "referrer_percent": 10000,
            "name": op_params["name"],
            "owner": {
                "weight_threshold": 1,
                "account_auths": [],
                "key_auths": [[op_params["owner_key"], 1]],
                "address_auths": []
            },
            "active": {
                "weight_threshold": 1,
                "account_auths": [],
                "key_auths": [[op_params["active_key"], 1]],
                "address_auths": []
            },
            "options": {
                "memo_key": op_params["memo_key"],
                "voting_account": "1.2.0",
                "num_witness": 0,
                "num_committee": 0,
                "votes": []
            },
            "extensions": []
        }
        return [5, account_create_payload]

    async def _build_op_withdraw_vesting(self, uid: str, op_params: dict) -> list:
        asset_symbol_or_id = op_params["asset"]
        if asset_symbol_or_id.startswith("1.3."):
            asset_id = asset_symbol_or_id
            _, _, asset_prec = await self.get_asset_brief(asset_id)
        else:
            _, asset_id, asset_prec = await self.get_asset_brief(asset_symbol_or_id)

        raw_amount = int(float(op_params["amount"]) * (10 ** asset_prec))

        withdraw_payload = {
            "fee": {"amount": 0, "asset_id": "1.3.0"},
            "vesting_balance": str(op_params["vesting_balance"]),
            "owner": str(uid),
            "amount": {
                "amount": raw_amount,
                "asset_id": asset_id
            }
        }
        return [33, withdraw_payload]

    async def encrypt_memo(self, op_params: dict) -> dict:
        if not self.key_manager:
            raise ValueError("密钥管理器未初始化")

        to_account_info = await self.get_account_info(op_params.get("to_account"))
        to_memo_pub = to_account_info.get('k', {}).get('m')
        if not to_memo_pub:
            raise ValueError(f"目标账号 [{op_params.get('to_account')}] 未在链上设置 Memo Key")

        my_account_info = await self.get_account_info(self.account_name)
        my_memo_pub = my_account_info.get('k', {}).get('m')
        my_active_keys = my_account_info.get('k', {}).get('a', [])

        chosen_my_pub = None
        if my_memo_pub and self.key_manager.has_key(my_memo_pub):
            chosen_my_pub = my_memo_pub
        else:
            for active_pub in my_active_keys:
                if self.key_manager.has_key(active_pub):
                    chosen_my_pub = active_pub
                    break

        if not chosen_my_pub:
            raise KeyError(
                f"当前账号 [{self.account_name}] 的 Memo Key ({my_memo_pub}) 及 Active Key ({my_active_keys}) "
                f"均未在本地密钥库中导入，无法签署加密 Memo！"
            )

        return self.key_manager.encrypt_memo(chosen_my_pub, to_memo_pub, op_params["memo"])

    async def decrypt_memo(self, memo_info: dict) -> str:
        if not self.key_manager:
            raise ValueError("密钥管理器未初始化")

        keys = memo_info.get('k', [])
        if not keys or len(keys) < 2:
            raise ValueError("Memo 数据不合法 (缺少双方公钥信息)")

        matched_my_pub = None
        for pub in keys:
            if self.key_manager.has_key(pub):
                matched_my_pub = pub
                break

        if not matched_my_pub:
            raise KeyError(f"链上 Memo 指定的接收/发送公钥对 {keys} 均不在当前本地私钥库中，无法解密")

        return self.key_manager.decrypt_memo(matched_my_pub, memo_info)