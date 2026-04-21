from .cattleguard import CattleGuard


async def setup(bot):
    await bot.add_cog(CattleGuard(bot))
