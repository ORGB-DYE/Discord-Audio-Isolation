# Discord-Audio-Isolation
Almost working audio isolation for native discord on linux


With this NVENC patch (https://github.com/relativemodder/discord-linux-vulkan-video-patcher) working with Vencord (https://vencord.dev/download/) the only thing I was missing was application audio isolation. 
_____________________________________

Instructions:

Install dependicies: 

Arch: sudo pacman -S python python-pip pipewire-utils libpulse python-pyqt6

Download the script and run it with: 

python3 discord-audio-isolation.py 

With the script running, start a screenshare on discord. 
After you select what window you want to share, a dialog popup will appear asking you which audio source you want to be streamed.

Known issues and limitations:
Currently it only blocks audio that is already playing on your computer. So if new application starts playing audio the stream will hear it. 
There is an issue with eacho/audio quality degradation when starting/stopping and streaming the same application. The temporary fix is to log out and back into your desktop session. 
Scrubbing the seek bar on some video players breaks the audio share.
