import os
import json
import time
import sqlite3
from typing import Tuple, Dict, Any, List, Optional, Callable
import asyncio

from btsbots.btsbots import BTSBots
from btsbots.utils import validate_bts_username

class SignBots(BTSBots):
    def __init__(self, db_path: str = "bots.sqlite", config_path: str = "security_rules.json"):
        super().__init__(db_path)
        self.config_path = config_path
        self.seen_signatures = set()
        self.last_config_mtime: float = 0.0
        self._init_db_order()

    def _init_db_order(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS successful_orders (
                    block_num TEXT PRIMARY KEY, account_id TEXT NOT NULL, op_type TEXT NOT NULL,
                    sell_asset_symbol TEXT NOT NULL, sell_amount INTEGER NOT NULL,
                    receive_asset_symbol TEXT NOT NULL, receive_amount INTEGER NOT NULL, timestamp INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS successful_transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    device_alias TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    amount REAL NOT NULL,
                    timestamp INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    account_name TEXT,
                    device_alias TEXT,
                    op_type TEXT,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    raw_summary TEXT
                )
            """)
            conn.commit()

    def _log_audit(self, account_name: str, device_alias: str, op_type: str, status: str, detail: str, raw_summary: dict = None):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO audit_logs (timestamp, account_name, device_alias, op_type, status, detail, raw_summary) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(time.time()),
                        account_name or "Unknown",
                        device_alias or "Unidentified",
                        op_type or "unknown",
                        status,
                        detail,
                        json.dumps(raw_summary, ensure_ascii=False) if raw_summary else "{}"
                    )
                )
                conn.commit()
        except Exception as e:
            print(f"⚠️ [审计日志写入异常]: {e}")

    async def hot_reload_config_file(self, force: bool = False) -> bool:
        try:
            if not os.path.exists(self.config_path):
                raise FileNotFoundError(f"Missing mandatory policy file: {self.config_path}")

            current_mtime = os.path.getmtime(self.config_path)
            if not force and (current_mtime <= self.last_config_mtime):
                return False

            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            print(f"🔄 配置文件加载成功。规则版本更新时间: {self.config.get('updated_at')}")
            self.last_config_mtime = current_mtime
            return True
        except Exception as err:
            print(f"❌ 配置文件解析发生异常: {str(err)}")
            return False

    async def hot_reload_strategy(self, force: bool = False):
        try:
            if not await self.hot_reload_config_file(force):
                return
            unlimited_cfg = self.config.get("unlimited_payments", {})
            user_whitelist = unlimited_cfg.get("recipient_whitelist", {})
            for username, claimed_id in list(user_whitelist.items()):
                _, _id = await self.get_account_brief(username)
                if _id and _id != claimed_id:
                    print(f"🚨 [风控警告] 账号 {username} 服务端ID {_id} 与配置的 '{claimed_id}' 不匹配！")
        except Exception as err:
            print(f"❌ 策略风控解析异常: {str(err)}")

    async def submit_proxy_sign_request(self, signed_envelope: dict) -> str:
        tx_id = await self.call("requestProxySign", signed_envelope)
        return str(tx_id)

    async def start_signature_queue_listener(self, on_broadcast_success: Optional[Callable[[str, str], None]] = None):
        await self.hot_reload_strategy()
        def _on_queue_income(action, collection, doc_id, fields):
            asyncio.create_task(self.hot_reload_strategy())
            if collection == 'proxy_sign_requests' and action == 'added':
                asyncio.create_task(self._audit_and_sign_worker(doc_id, fields, on_broadcast_success))
            elif collection == 'account_registrations' and action == 'added':
                asyncio.create_task(self._process_account_registration(doc_id, fields))

        self.on_data_changed = _on_queue_income
        await self.subscribe("allPendingSignRequests")
        await self.subscribe("pendingAccountRegistrations")
        print("⚡ [BTSBots 签名网关] 零信任安全守卫与邀请注册监听器已全面启动...\n")

    async def _process_account_registration(self, doc_id: str, fields: dict):
        new_account_name = fields.get("newAccountName")
        keys = fields.get("keys", {})
        registrar = fields.get("registrar")

        if registrar != self.account_name:
            return

        print(f"\n────────────────────────────────────────────────────────────")
        print(f"👤 [收到新用户注册申请]")
        print(f"   - 申请用户名: {new_account_name}")
        print(f"   - 使用邀请码: {fields.get('code')}")

        try:
            is_valid, err_msg = validate_bts_username(new_account_name)
            if not is_valid:
                raise ValueError(f"用户名不合规: {err_msg}")

            owner_key = keys.get("owner")
            active_key = keys.get("active")
            memo_key = keys.get("memo")

            raw_op = {
                "type": "account_create",
                "params": {
                    "name": new_account_name,
                    "owner_key": owner_key,
                    "active_key": active_key,
                    "memo_key": memo_key
                }
            }

            print(f"   🚀 正在代为向链上广播注册交易 (推荐人与注册人自动设为: {self.account_name})...")
            block_num = await self.make_transaction([raw_op], isSim=False)

            print(f"   ✅ 注册成功！已写入区块链，区块号: {block_num}")
            print(f"────────────────────────────────────────────────────────────\n")

            await self.call("resolveAccountRegistration", doc_id, True, f"Block: {block_num}")

        except Exception as err:
            error_msg = str(err)
            print(f"   ❌ 注册代办失败: {error_msg}")
            print(f"────────────────────────────────────────────────────────────\n")
            await self.call("resolveAccountRegistration", doc_id, False, error_msg)

    def _format_readable_summary(self, op_type: str, params: dict) -> tuple[str, dict]:
        summary_desc = ""
        human_data = {}

        if op_type == "transfer":
            to_acc = params.get("to_account")
            amount = params.get("amount")
            asset = params.get("asset")
            memo = params.get("memo", "")
            summary_desc = f"向账户 [{to_acc}] 转账 {amount} {asset}" + (f" (附言: {memo})" if memo else "")
            human_data = {"目标账户": to_acc, "金额": f"{amount} {asset}", "附言": memo}

        elif op_type == "limit_order_create":
            sell_amt = params.get("amount")
            sell_ast = params.get("sell_asset")
            recv_ast = params.get("receive_asset")
            price = params.get("price")
            summary_desc = f"挂单出售 {sell_amt} {sell_ast}，换取 {recv_ast}，单价: {price}"
            human_data = {"出售": f"{sell_amt} {sell_ast}", "购买资产": recv_ast, "价格": price}

        elif op_type == "limit_order_cancel":
            order_id = params.get("order_id")
            summary_desc = f"撤销限价订单 [ID: {order_id}]"
            human_data = {"订单ID": order_id}

        elif op_type == "withdraw_vesting":
            vb_id = params.get("vesting_balance")
            amount = params.get("amount")
            asset = params.get("asset")
            summary_desc = f"提现归属余额 [{vb_id}] 金额: {amount} {asset}"
            human_data = {"归属余额ID": vb_id, "金额": f"{amount} {asset}"}

        elif op_type == "oauth_login":
            site = params.get("site")
            client_id = params.get("client_id")
            summary_desc = f"授权登录第三方网站 [{site}] (客户端ID: {client_id})"
            human_data = {"目标网站": site, "商户ID": client_id}
        else:
            summary_desc = f"执行未知操作类型: {op_type}"
            human_data = params

        return summary_desc, human_data

    async def _audit_and_sign_worker(self, doc_id: str, fields: dict, success_callback: Optional[Callable]):
        envelope = fields.get("rawTx", {})
        account_name = fields.get("account", "Unknown")
        authenticated_sender_id = fields.get("account_id")

        is_valid_crypto, device_alias, crypto_msg = self._verify_browser_envelope(envelope)

        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"────────────────────────────────────────────────────────────")
        print(f"📥 [收到签名请求] 时间: {time_str}")
        print(f"   👤 请求账号: {account_name} (ID: {authenticated_sender_id})")
        print(f"   💻 请求设备: {device_alias or '未识别设备'}")

        if not is_valid_crypto:
            print(f"   ❌ 状态结果: 【拦截拒绝】")
            print(f"   🚨 拒绝原因: {crypto_msg}")
            print(f"────────────────────────────────────────────────────────────\n")
            self._log_audit(account_name, device_alias, "unknown", "REJECTED", crypto_msg)
            await self.call('replySignRequest', False, doc_id, f"{crypto_msg}")
            return

        raw_payload = json.loads(envelope.get("tx_string", "{}"))
        op_type = raw_payload.get("type", "unknown")
        op_params = raw_payload.get("params", {})
        pin_code_provided = op_params.get("pin")

        readable_desc, readable_data = self._format_readable_summary(op_type, op_params)
        print(f"   📋 操作摘要: {readable_desc}")

        if op_type == "oauth_login":
            oauth_devices = self.config.get("oauth_allowed_devices", [])
            if device_alias not in oauth_devices:
                reason = f"设备 [{device_alias}] 无权执行 OAuth 授权登录"
                print(f"   ❌ 状态结果: 【拦截拒绝】")
                print(f"   🚨 拒绝原因: {reason}")
                print(f"────────────────────────────────────────────────────────────\n")
                self._log_audit(account_name, device_alias, op_type, "REJECTED", reason, readable_data)
                await self.call('replySignRequest', False, doc_id, reason)
                return

            print(f"   ✅ 状态结果: 【审核通过 - 准备执行 OAuth 授权】")
            print(f"────────────────────────────────────────────────────────────\n")
            self._log_audit(account_name, device_alias, op_type, "APPROVED", "OAuth 授权成功", readable_data)
            await self.oauth_handle(doc_id, fields)
            return

        is_safe, fee_msg = self._check_fee_doc([0, 1, 2, 5, 33])
        if not is_safe:
            print(f"   ❌ 状态结果: 【拦截拒绝】")
            print(f"   🚨 拒绝原因: {fee_msg}")
            print(f"────────────────────────────────────────────────────────────\n")
            self._log_audit(account_name, device_alias, op_type, "REJECTED", fee_msg, readable_data)
            await self.call('replySignRequest', False, doc_id, fee_msg)
            return

        is_safe, status_msg, require_pin, pin_error = await self._audit_security_strategy(
            op_type, op_params, authenticated_sender_id, device_alias, pin_code_provided
        )

        if require_pin:
            print(f"   ⚠️ 状态结果: 【触发 PIN 码验证】")
            print(f"   🔒 提示信息: {status_msg}")
            print(f"────────────────────────────────────────────────────────────\n")
            self._log_audit(account_name, device_alias, op_type, "REQUIRE_PIN", status_msg, readable_data)
            await self.call('replySignRequest', False, doc_id, {"requirePin": True, "message": status_msg})
            return

        if not is_safe:
            print(f"   ❌ 状态结果: 【审核拦截】")
            print(f"   🚨 拒绝原因: {status_msg}")
            print(f"────────────────────────────────────────────────────────────\n")
            self._log_audit(account_name, device_alias, op_type, "REJECTED", status_msg, readable_data)
            await self.call('replySignRequest', False, doc_id, status_msg)
            return

        print(f"   ✅ 状态结果: 【审核通过 - 正在链上签名广播】")
        print(f"────────────────────────────────────────────────────────────\n")

        try:
            is_sim = op_params.get("simulate", False)
            block_num = await self.make_transaction([raw_payload], is_sim)

            self._log_audit(account_name, device_alias, op_type, "SUCCESS", f"交易已成功打包，区块号: {block_num}", readable_data)

            if op_type == "transfer":
                self._log_successful_transfer(authenticated_sender_id, device_alias, op_params.get("asset"), float(op_params.get("amount")))

            await self.call('replySignRequest', True, doc_id, block_num)
            if success_callback:
                success_callback(doc_id, block_num)

            if op_type == "limit_order_create":
                self._log_successful_transaction(
                    block_num=block_num,
                    account_id=authenticated_sender_id,
                    op_type=op_type,
                    params=op_params
                )
        except Exception as e:
            error_reason = f"网关异常中断: {str(e)}"
            print(f"🚨 [广播失败] {error_reason}")
            self._log_audit(account_name, device_alias, op_type, "ERROR", error_reason, readable_data)
            await self.call('replySignRequest', False, doc_id, error_reason)

    async def oauth_handle(self, doc_id: str, fields: dict):
        envelope = fields.get("rawTx", {})
        clientIp = fields.get("clientIp")

        raw_payload = json.loads(envelope.get("tx_string", "{}"))
        op_params = raw_payload.get("params", {})

        client_id = op_params.get("client_id")
        token = op_params.get("token")
        site = op_params.get("site")
        ip = op_params.get("ip")

        if not client_id or not token or not site or not ip:
            msg = "授权登录参数不完整"
            await self.call('replySignRequest', False, doc_id, msg)
            return

        if clientIp != ip:
            msg = "BTSBots client 与商户 client IP 地址不符合"
            await self.call('replySignRequest', False, doc_id, msg)
            return

        try:
            # 🌟 显式定位 Active Key 签名
            active_pub = await self._resolve_account_active_pubkey(self.account_name)
            auth_payload = self._generate_auth_payload(self.account_name, active_pub, site, token, ip)
            final_payload = {
                "client_id": str(client_id).lower().strip(),
                "verify": auth_payload.get("verify")
            }

            print(f"📡 正在通过 RPC 向 Meteor 服务器推送授权凭证到商户端...")
            await self.call('pushAuthToMerchantQueue', final_payload)
            print(f"✅ [OAuth 登录成功] 站点 [{site}] 已获得授权。")

            await self.call('replySignRequest', True, doc_id, token)
            return
        except Exception as oauth_err:
            error_msg = f"授权背书签名或 RPC 推送发生故障: {str(oauth_err)}"
            print(f"❌ [OAuth 授权失败]: {error_msg}")
            await self.call('replySignRequest', False, doc_id, error_msg)
            return

    def _check_fee_doc(self, op_types: list) -> tuple[bool, str]:
        global_coll = self.collections.get("global", {})
        if not global_coll:
            raise KeyError("本地内存中暂未接收到同步的费率数据。")
        global_doc = next((doc for doc in global_coll.values() if doc.get("id") == "2.0.0"))
        fee_doc = global_doc["parameters"].get("current_fees", {}).get("parameters", [])
        fee_map = {item[0]: item[1] for item in fee_doc if isinstance(item, list) and len(item) == 2}
        fee_limit = self.config.get("fee_limit", 10)
        for index in op_types:
            item = fee_map[index]
            if index == 5:
                fee = (item.get("basic_fee") + 0.1*item.get("price_per_kbyte")) / 10**5
            elif index == 0:
                fee = (item.get("fee") + 0.1*item.get("price_per_kbyte")) / 10**5
            else:
                fee = int(item.get("fee")) / 10**5
            if fee > fee_limit:
                return False, f"交易费用 {fee} BTS，超过风控限制 ({fee_limit} BTS)"
        return True, ""

    def _verify_browser_envelope(self, envelope: dict) -> tuple[bool, Optional[str], str]:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes
        from binascii import unhexlify
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        import hashlib
        import json
        import time

        tx_string = envelope.get("tx_string")
        pubkey_hex = envelope.get("browser_pubkey")
        signature_hex = envelope.get("browser_sig")

        if not tx_string or not pubkey_hex or not signature_hex:
            return False, None, "解包失败：缺少标准 Web Crypto 鉴权参数。"

        try:
            hasher = hashlib.sha256()
            hasher.update(pubkey_hex.lower().encode('utf-8'))
            computed_fingerprint_50 = hasher.hexdigest()[:50]

            public_keys_map = self.config.get("public_keys", {})
            if computed_fingerprint_50 not in public_keys_map:
                return False, None, f"未授权的设备公钥指纹: {computed_fingerprint_50}"

            device_alias = public_keys_map[computed_fingerprint_50]

            public_key_bytes = unhexlify(pubkey_hex)
            native_pubkey_object = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), public_key_bytes
            )

            signature_bytes = unhexlify(signature_hex)
            if len(signature_bytes) == 64:
                r = int.from_bytes(signature_bytes[:32], byteorder='big')
                s = int.from_bytes(signature_bytes[32:], byteorder='big')
                der_signature = encode_dss_signature(r, s)
            else:
                return False, device_alias, "无效的签名数据长度"

            native_pubkey_object.verify(
                der_signature,
                tx_string.encode('utf-8'),
                ec.ECDSA(hashes.SHA256())
            )
        except Exception as crypto_err:
            return False, None, f"密码学签名验证异常: {crypto_err}"

        try:
            raw_payload = json.loads(tx_string)
        except Exception:
            return False, None, "无法解析交易内容 JSON"

        if abs(int(time.time()) - raw_payload.get("client_time", 0)) > 30:
            return False, device_alias, "交易请求已超时过期"

        if signature_hex in self.seen_signatures:
            return False, device_alias, "检测到重放攻击：重复的交易请求"
        self.seen_signatures.add(signature_hex)

        return True, device_alias, "签名可信"

    async def _audit_security_strategy(self, op_type: str, params: dict, sender_id: str, device_alias: str, pin_code_provided: Optional[str]) -> Tuple[bool, str, bool, str]:
        try:
            if op_type == "withdraw_vesting":
                unlimited_cfg = self.config.get("unlimited_payments", {})
                authorized_devices = unlimited_cfg.get("authorized_devices", [])
                if device_alias not in authorized_devices:
                    return False, f"设备 [{device_alias}] 没有权限执行提现分红 (withdraw_vesting) 操作", False, ""
                
                if not params.get("vesting_balance") or not params.get("amount") or not params.get("asset"):
                    return False, "withdraw_vesting 参数不完整 (缺少 vesting_balance, amount 或 asset)", False, ""

                return True, "提现分红安全策略校验通过", False, ""

            if op_type == "transfer":
                unlimited_cfg = self.config.get("unlimited_payments", {})
                authorized_devices = unlimited_cfg.get("authorized_devices", [])
                recipient_whitelist = unlimited_cfg.get("recipient_whitelist", {})

                to_account_name = params.get("to_account")

                if device_alias in authorized_devices and to_account_name in recipient_whitelist:
                    return True, "大额白名单转账策略校验通过", False, ""

                micro_cfg = self.config.get("micro_payments", {})
                base_limits = micro_cfg.get("base_limits", {})
                device_rules = micro_cfg.get("device_rules", {})

                if device_alias not in device_rules:
                    return False, f"设备 [{device_alias}] 不在小额支付允许的设备列表中", False, ""

                dev_rule = device_rules[device_alias]
                asset = params.get("asset", "").upper().strip()
                amount = float(params.get("amount", 0))

                base_limit = base_limits.get(asset, 0)
                if base_limit == 0:
                    return False, f"资产 [{asset}] 未配置小额支付基准额度", False, ""

                single_limit = base_limit * dev_rule.get("single_multiplier", 0)
                day_limit = base_limit * dev_rule.get("day_max_multiplier", 0)
                week_limit = base_limit * dev_rule.get("week_max_multiplier", 0)

                if amount > single_limit:
                    return False, f"转账金额 {amount} {asset} 超出单笔小额限额 ({single_limit})", False, ""

                if not self._check_micro_payment_accumulated(sender_id, device_alias, asset, amount, day_limit, week_limit):
                    return False, f"转账金额超出小额【天累计】或【周累计】限额", False, ""

                required_pin = dev_rule.get("pin")
                if required_pin is not None and str(required_pin) != "":
                    if not pin_code_provided:
                        return False, f"该小额转账操作安全级别较高，需要输入 PIN 码验证", True, ""
                    elif str(pin_code_provided) != str(required_pin):
                        return False, f"输入的 PIN 码错误，拒绝签名", False, "PIN码错误"

                return True, "小额支付策略校验通过（含PIN验证）", False, ""

            if op_type == "limit_order_cancel":
                return True, "撤单安全策略校验通过", False, ""

            return True, "操作安全策略校验通过", False, ""

        except Exception as err:
            return False, f"风控策略解析异常: {str(err)}", False, ""

    def _check_micro_payment_accumulated(self, account_id: str, device_alias: str, asset: str, current_amount: float, day_max: float, week_max: float) -> bool:
        now = int(time.time())
        day_ago = now - 86400
        week_ago = now - 604800

        with sqlite3.connect(self.db_path) as conn:
            cursor_day = conn.execute("""
                SELECT SUM(amount) FROM successful_transfers
                WHERE account_id = ? AND device_alias = ? AND asset = ? AND timestamp >= ?
            """, (account_id, device_alias, asset, day_ago))
            day_sum = cursor_day.fetchone()[0] or 0.0

            if (day_sum + current_amount) > day_max:
                return False

            cursor_week = conn.execute("""
                SELECT SUM(amount) FROM successful_transfers
                WHERE account_id = ? AND device_alias = ? AND asset = ? AND timestamp >= ?
            """, (account_id, device_alias, asset, week_ago))
            week_sum = cursor_week.fetchone()[0] or 0.0

            if (week_sum + current_amount) > week_max:
                return False

        return True

    def _log_successful_transfer(self, account_id: str, device_alias: str, asset: str, amount: float):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO successful_transfers (account_id, device_alias, asset, amount, timestamp) VALUES (?, ?, ?, ?, ?)",
                (account_id, device_alias, asset.upper().strip(), amount, int(time.time()))
            )
            conn.commit()

    def _log_successful_transaction(self, block_num: str, account_id: str, op_type: str, params: dict):
        if op_type != "limit_order_create":
            return
        sell_symbol = str(params["sell_asset"]).upper().strip()
        recv_symbol = str(params["receive_asset"]).upper().strip()
        sell_amount = float(params["amount"])
        recv_amount = float(params["amount"]) * float(params["price"])

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO successful_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                block_num, account_id, op_type,
                sell_symbol, sell_amount,
                recv_symbol, recv_amount,
                int(time.time())
            ))
            conn.commit()
