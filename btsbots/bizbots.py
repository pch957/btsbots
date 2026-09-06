import os
import json
import asyncio
import requests
from btsbots.btsbots import BTSBots
import re

def get_domain(url):
    return re.sub(r'^(https?://)?([^/]+).*$', r'\2', url)

class BizBots(BTSBots):
    def __init__(self, db_path: str = "bots.sqlite", config_path: str = "biz_rules.json"):
        super().__init__(db_path)
        self.config_path = config_path
        self.last_config_mtime: float = 0.0
        self.oauth_endpoint = {}
        self.pay_endpoint = {}

    async def hot_reload_config_file(self, force: bool = False):
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
            print(f"配置文件解析发生异常: {str(err)}")

    async def hot_reload_strategy(self, force: bool = False):
        try:
            if not await self.hot_reload_config_file(force):
                return
            oauth_endpoint = self.config.get("oauth_endpoint")
            if isinstance(oauth_endpoint, str):
                domain = get_domain(oauth_endpoint)
                self.oauth_endpoint = { domain: oauth_endpoint }
            elif isinstance(oauth_endpoint, dict):
                for domain in oauth_endpoint:
                    if domain != get_domain(oauth_endpoint[domain]):
                        print(f"❌[配置错误] 域名不符，忽略 oauth 回调地址: {oauth_endpoint[domain]}")
                        continue
                    self.oauth_endpoint[domain] = oauth_endpoint[domain]

            pay_endpoint = self.config.get("pay_endpoint")
            if isinstance(pay_endpoint, str):
                self.pay_endpoint = { "": pay_endpoint }
            elif isinstance(pay_endpoint, dict):
                self.pay_endpoint = pay_endpoint
        except Exception as err:
            print(f"配置文件解析发生异常: {str(err)}")

    async def start_oauth_queue_listener(self):
        await self.hot_reload_strategy()
        def _on_queue_income(action, collection, doc_id, fields):
            asyncio.create_task(self.hot_reload_strategy())
            if collection == 'login_requests' and action == 'added':
                asyncio.create_task(self._verify_and_sign_oauth(doc_id, fields))
            elif collection == 'transfer' and action == 'added':
                asyncio.create_task(self._verify_and_sign_pay(doc_id, fields))

        self.on_data_changed = _on_queue_income
        await self.subscribe("OauthLoginRequests")
        await self.subscribe("myTransfers")
        print("⚡[BTSBots] 零信任安全守卫，商务网关，正在监听中...")

    async def _verify_and_sign_pay(self, doc_id: str, fields: dict):
        if not self.pay_endpoint:
            return
        import time

        try:
            delta_time = 0
            if isinstance(fields.get('T'), dict):
                delta_time = time.time() - fields.get('T')['$date']/1000
            else:
                delta_time = time.time() - fields.get('T')

            if delta_time > 60*5:
                return
            users = fields.get('u')
            if users[1] != self.bts_id:
                return
            if not fields.get('m'):
                return

            tx_id = int(doc_id[1:]) if doc_id.startswith("1.11.") or doc_id.startswith("1.12.") else doc_id
            memo = fields.get('m')
            
            # 使用统一的 key_manager 解密 memo (传入本机解密公钥与 memo 对象)
            account_info = await self.get_account_info(self.account_name)
            my_memo_pub = account_info.get('k', {}).get('m')
            if not my_memo_pub:
                print("❌ [支付解密失败] 当前账号未配置 Memo 公钥")
                return

            memo_str = self.key_manager.decrypt_memo(my_memo_pub, memo)

            post_data = {
                "type": "payment",
                "time": int(time.time()),
                "order_id": memo_str,
                "tx_id": str(tx_id),
                "amount": fields.get('b'),
                "asset": fields.get('a')
            }
            
            # 🌟 修复：必须显式传入有效的签名公钥给 key_manager.sign_message
            active_pub = await self._resolve_account_active_pubkey(self.account_name)
            payload = self.key_manager.sign_message(active_pub, json.dumps(post_data, sort_keys=True))

            for pattern in self.pay_endpoint:
                if memo_str.startswith(pattern):
                    response = requests.post(self.pay_endpoint[pattern], json=payload, timeout=5)
                    if response.status_code == 200:
                        print(f"✅ [支付成功]: 网站已成功收到账单通知，网页端即将放行！")
                        return True
                    else:
                        print(f"❌ [通知失败]: 网站后端返回了异常代码 HTTP{response.status_code} {response.text}")
                        return False
            print(f"❌ [通知失败]: 未找到匹配本订单的回调地址: {tx_id}")
            return False
        except Exception as e:
            print(f"❌ [_verify_and_sign_pay 异常]: {e}")
            return False

    async def _verify_and_sign_oauth(self, doc_id: str, fields: dict):
        import time
        import math
        status = "fail"
        if not self.oauth_endpoint:
            return
        try:
            verify = fields.get("verify")
            # 使用统一的 key_manager 验证消息签名
            if not self.key_manager.verify_message(verify):
                print(f"❌ [授权失败]: 签名错误")
                return
            
            raw_payload = json.loads(verify.get("data"))
            account = raw_payload.get("username")
            account_info = await self.get_account_info(account)
            
            if verify.get("pubkey") not in account_info.get('k', {}).get('a', []):
                print(f"❌ [授权失败]: active key 没找到签名公钥")
                return

            domain = get_domain(raw_payload.get("site"))
            if domain not in self.oauth_endpoint:
                print(f"❌ [授权失败]: 本网址未指定回调地址: {domain}")
                return

            timeout = int(math.fabs(time.time() - raw_payload.get("time")))
            if timeout >= 2 * 60:  # 修正毫秒为秒的合理范围
                print(f"❌ [授权失败]: token 超时 {timeout} 秒")
                return

            print(f"✅ [检查通过]: 用户 {raw_payload.get('username')} 取得 {domain} 合法授权")
            print(f"📡 [等待登录]: 正在通知商户网站...")

            # 🌟 修复：显式使用 Active Key 对数据进行签名并提交商户端
            active_pub = await self._resolve_account_active_pubkey(self.account_name)
            payload = self.key_manager.sign_message(active_pub, verify.get("data"))

            response = requests.post(self.oauth_endpoint[domain], json=payload, timeout=5)

            if response.status_code == 200:
                print(f"✅ [登录成功]: 商户网站已成功接收到登录授权！")
                status = "success"
                return
            else:
                print(f"❌ [通知失败]: 网站后端返回了异常代码 HTTP {response.status_code}")
                return
        except Exception as e:
            print(f"❌ [_verify_and_sign_oauth 异常]: {e}")
        finally:
            await self.call('SubmitOauthLoginRequest', doc_id, status)