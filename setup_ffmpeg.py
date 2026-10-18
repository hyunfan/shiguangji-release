"""Install optional video tools from a Windows provider linked by ffmpeg.org."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parent
URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'


def install():
    target = ROOT/'tools'/'ffmpeg'
    target.mkdir(parents=True, exist_ok=True)
    with urlopen(URL+'.sha256',timeout=45) as response:
        expected = response.read(4096).decode('ascii').split()[0].lower()
    if not re.fullmatch('[a-f0-9]{64}',expected):
        raise ValueError('视频组件校验信息无效。')
    with tempfile.TemporaryDirectory(prefix='ffmpeg-',dir=target) as temporary:
        package=Path(temporary)/'package.zip';size=0;sha=hashlib.sha256()
        print('Downloading verified FFmpeg video tools...',flush=True)
        with urlopen(URL,timeout=60) as response, package.open('wb') as writer:
            while block:=response.read(1024*1024):
                size+=len(block)
                if size>300*1024**2:raise ValueError('视频组件下载超过大小限制。')
                sha.update(block);writer.write(block)
        if sha.hexdigest()!=expected:raise ValueError('视频组件下载校验失败，未安装。')
        installed={}
        with zipfile.ZipFile(package) as archive:
            for name in ('ffmpeg.exe','ffprobe.exe','LICENSE'):
                matches=[entry for entry in archive.infolist() if Path(entry.filename).name==name and not entry.is_dir()]
                if len(matches)!=1 or matches[0].file_size>300*1024**2:raise ValueError('视频组件包结构不符。')
                output=Path(temporary)/name
                with archive.open(matches[0]) as reader,output.open('wb') as writer:shutil.copyfileobj(reader,writer)
                installed[name]=hashlib.sha256(output.read_bytes()).hexdigest()
                output.replace(target/name)
        (target/'origin.json').write_text(json.dumps({'source':URL,'package_sha256':expected,'files':installed},indent=2),encoding='utf-8')
    print('Video tools installed: '+str(target),flush=True)


if __name__=='__main__':install()
