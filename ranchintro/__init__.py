from .ranchintro import RanchIntro


async def setup(bot):
    await bot.add_cog(RanchIntro(bot))
