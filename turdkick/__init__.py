from .turdkick import TurdKick


async def setup(bot):
    await bot.add_cog(TurdKick(bot))
