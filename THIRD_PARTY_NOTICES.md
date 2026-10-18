# 第三方组件、来源与许可

本文件列出运行及构建所需的外部组件，不取代组件自身的完整许可证。本源码仓库不包含下列组件的二进制发行包，也不包含从外部网站采集的帖子或媒体。

本项目代码采用根目录 [LICENSE](LICENSE) 中的 MIT 许可证。该许可不替代下列组件自身的许可，也不授予第三方内容的使用或传播权。

| 组件或服务 | 使用方式 | 来源与许可信息 |
|---|---|---|
| Python / CPython | 源码运行环境；便携构建下载官方 Windows 嵌入式包 | [Python 许可说明](https://docs.python.org/3/license.html)。构建保留官方 `python/LICENSE.txt`；`python-runtime.json` 固定下载来源和 SHA-256，`portable.json` 记录包来源。 |
| FFmpeg / FFprobe | 可选独立进程，用于本地视频合并和校验 | [FFmpeg 许可说明](https://ffmpeg.org/legal.html)。不同构建及启用组件可能使用 LGPL 或 GPL，必须按实际发行包许可证处理。安装脚本从 [FFmpeg 下载页](https://ffmpeg.org/download.html)列出的 [Gyan 构建](https://www.gyan.dev/ffmpeg/builds/)下载并保留 `LICENSE`、`origin.json`；本仓库和标准便携包不内置这些文件。 |
| Node.js | 仅用于开发测试；终端用户运行应用不需要 | [Node.js 许可证](https://github.com/nodejs/node/blob/main/LICENSE)。仓库使用其内置测试运行器，不包含 Node.js 二进制或第三方 npm 包。 |
| Wayback Machine、Common Crawl、X、Instagram 及公开元数据来源 | 通过各自网络接口查询可访问资料 | 这些服务不是本项目的一部分，其名称不表示官方认可或合作。使用须遵循适用条款、访问限制及内容权利。 |

将可选视频组件或 Python 与其他发布物重新打包时，须重新核对该发行包的完整许可、版权通知及适用源码提供要求；不能仅附上本文件代替它们。程序代码的许可也不会授予第三方帖子、图片、音视频的使用或传播权利。
