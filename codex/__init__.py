from codex.core.bot import Anony
from codex.core.dir import dirr
from codex.core.git import git
from codex.core.userbot import Userbot
from codex.misc import dbb, heroku

from .logging import LOGGER

dirr()
git()
dbb()
heroku()

app = Anony()
userbot = Userbot()

from .platforms import *

Apple = AppleAPI()
Carbon = CarbonAPI()
SoundCloud = SoundAPI()
Spotify = SpotifyAPI()
Resso = RessoAPI()
Telegram = TeleAPI()
YouTube = YouTubeAPI()
JioSaavn = JioSaavnAPI()
