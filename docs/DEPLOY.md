# 部署

中文 | [English](DEPLOY.en.md)

服务端是一个 Docker 容器，用官方的 `python:3.12-slim` 镜像，把 `ktv-server/` 目录只读挂进去运行，不用构建镜像。
任何能跑 Docker Compose 的机器都可以。

## 第一次部署

```bash
git clone https://github.com/jackyierfati/home-ktv.git
cd home-ktv
cp .env.example .env
docker compose up -d
```

`.env` 里常改的几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `KTV_MEDIA` | `./media` | 歌曲文件目录。一首 50 ~ 80 MB，放在大硬盘上 |
| `KTV_DATA` | `./data` | 曲库、设置、已点、下载队列 |
| `KTV_PORT` | `8787` | 点歌网页、管理页、电视都用这个端口 |
| `KTV_NAME` | `家庭KTV` | 电视主页和点歌页上的名字（管理页里也能改） |
| `KTV_THREADS` | `64` | 每首歌同时开几路连接 |
| `KTV_PARALLEL` | `2` | 同时下几首歌 |
| `KTV_PROXY` | 空 | 下载用的 HTTP 代理，见下一节 |

然后：

1. 打开 `http://<这台机器的 IP>:8787/admin` →「曲库与设置」→「更新曲库」。曲库有 8 万多首，第一次要几分钟。
2. 同一页里填「名字」和 WiFi（可选，电视主页会多一个「连 WiFi」二维码）。
3. 电视装播放器，见 [ktv-player/README.md](../ktv-player/README.md)。

检查：

```bash
curl http://127.0.0.1:8787/health          # {"ok": true, "version": "3"}
docker logs --tail 50 home-ktv
```

## 让下载走中国大陆出口（在国外时）

歌源在国内。海外直连时常常很慢，或者取不到下载地址。只让下载走中国大陆出口就能解决，家里其他设备不受影响。

**已经有中国大陆出口的 HTTP 代理**：在 `.env` 里填 `KTV_PROXY=http://代理地址:端口`，然后 `docker compose up -d`。

**用 WireGuard**（任何有中国大陆节点、能导出 WireGuard 配置的 VPN 都行）：

1. 从 VPN 服务商那里下载一个中国大陆节点的 WireGuard 配置文件。
2. 把配置里的值填进 `.env`：

   ```ini
   KTV_PROXY=http://vpn:8888
   WIREGUARD_ENDPOINT_IP=     # [Peer] Endpoint 的 IP
   WIREGUARD_ENDPOINT_PORT=51820
   WIREGUARD_PUBLIC_KEY=      # [Peer] PublicKey
   WIREGUARD_ADDRESSES=       # [Interface] Address，只填 IPv4，比如 10.2.0.2/32
   ```

3. 私钥（`[Interface] PrivateKey`）单独存成文件，不要写进 `.env`：

   ```bash
   mkdir -p secrets
   printf '%s' '<你的私钥>' > secrets/wireguard_private_key
   chmod 600 secrets/wireguard_private_key
   ```

4. 带上 `vpn` 启动：`docker compose --profile vpn up -d`

确认下载确实走了 VPN（应该显示中国大陆的 IP）：

```bash
docker exec home-ktv python -c "import urllib.request;print(urllib.request.urlopen('https://ipinfo.io/json',timeout=15).read().decode())"
```

代理只给下载用：服务端访问局域网（比如电视上的麦动）时不走代理。

## 更新

```bash
git pull
docker compose restart ktv
```

**有人在唱歌的时候不要重启**，会把正在放的歌打断。先看一眼 `http://<IP>:8787/api/room` 里 `"current"` 是不是 `null`。
只改了 `ktv-server/server/web/` 下的网页时不用重启：刷新页面就行，电视主页会自己刷新。

## TrueNAS SCALE

在 TrueNAS 上按上面的方式用就行，几点提示：

- 把仓库放在一个数据集里，比如 `/mnt/<池>/apps/home-ktv`，在 Shell 里 `sudo docker compose up -d`。
  也可以在「Apps → Discover Apps → Install via YAML」里粘贴 `compose.yaml`，这时要把 `./ktv-server` 等相对路径改成绝对路径。
- `KTV_MEDIA` 指向放歌的数据集，比如 `/mnt/<池>/media/KTV`。这个目录也可以同时共享给 SMB 或 OpenList。
- 曲库和设置放在 `apps` 数据集下，不要和歌曲放在一起。

## 修复旧版下载器下的歌

i歌霸 把 HTTP Range 的右端点当成「不包含」，本项目 2026 年 10 月之前的下载器每段都少了最后 1 个字节，
播放到一半会花屏或报错。现在的下载器已经修好。旧文件可以原地补回：

```bash
docker exec home-ktv python -u -m server.repair --data /data --media /media
```

修过的文件记在 `data/repaired.json`，再跑会跳过。补不回来的，在管理页搜到这首歌点「重新下载」。

## 配合麦动 KTV 使用（可选）

如果电视上已经在用 [麦动 KTV](https://gitee.com/yangyachao-X/maidong-ktv)，可以在管理页「曲库与设置 → 电视播放器」选「麦动」，
填上麦动的局域网地址（`http://电视的IP:8765`）。这时点歌和遥控转给麦动，NAS 只负责下载。
