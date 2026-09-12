import asyncio
import argparse
from btsbots.bots_client import BotsClient

class InvitationTool(BotsClient):
    def extend_arguments(self, parser):
        super().extend_arguments(parser)
        parser.add_argument("--action", type=str, required=True, choices=["generate", "list"], help="操作类型: generate(生成) 或 list(查看)")
        parser.add_argument("--count", type=int, default=1, help="批量生成的邀请码数量 (默认 1 个)")

    async def run(self):
        await super().run()
        args = self.args
        try:
            print(f"[*] 正在核验账号 [{self.account_name}] 的 VIP 会员资格...")
            account_info = await self.get_account_info(self.account_name)
            is_vip = account_info.get("v", False)

            if args.action == "generate":
                if not is_vip:
                    print("==================================================")
                    print(" ❌ 权限拒绝：您当前不是 VIP 用户（v: false）！")
                    print(" 💡 提示：只有升级为 VIP 用户后才具备生成邀请码和代注册账户的资格。")
                    print("==================================================")
                    return

                print(f"[*] 正在为 VIP 账号 [{self.account_name}] 批量生成 {args.count} 个邀请码...")
                res = await self.call("generateInvitation", args.count)
                
                # 兼容防错：如果服务端返回的恰好是 string（防止历史旧版缓存），转为 dict 处理
                if isinstance(res, str):
                    res = {"codes": [res], "generated": 1, "message": "生成成功"}

                print("==================================================")
                print(f" ✨ {res.get('message', '生成成功')} (成功生成 {res.get('generated', 1)} 个):")
                print("==================================================")
                for code in res.get("codes", []):
                    print(f"  • {code}")
                print("==================================================")

            elif args.action == "list":
                print(f"[*] 正在通过 RPC 获取账号 [{self.account_name}] 的邀请码列表...")
                invitations = await self.call("listInvitations")
                
                print("==================================================")
                print(f" 📋 邀请码列表 (共 {len(invitations)} 个) | VIP 状态: {'🟢 已激活' if is_vip else '🔴 未开通'}:")
                print("==================================================")
                for inv in invitations:
                    status = "🟢 已使用" if inv.get("status") == "used" else "🔵 未使用"
                    used_by = f" -> 注册用户: {inv.get('usedBy')}" if inv.get("usedBy") else ""
                    print(f" • 邀请码: {inv.get('code')} | 状态: {status}{used_by}")
                print("==================================================")
        except Exception as err:
            print(f"❌ 操作失败: {err}")
        finally:
            await self.close()

async def main():
    tool = InvitationTool()
    await tool.run()

if __name__ == "__main__":
    asyncio.run(main())
