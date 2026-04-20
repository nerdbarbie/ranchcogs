from .impostergate import ImposterGate


async def setup(bot):
    await bot.add_cog(ImposterGate(bot))
