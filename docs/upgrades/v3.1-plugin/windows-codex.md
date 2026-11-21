# 在 Windows Codex 中使用 VKS

准备一台 x64 Windows 电脑、ChatGPT 桌面应用中的 Codex，以及一份 Studio 导出的只读成品库 HTML。原生包内含本地向量模型。查询离线成品库只需要选择 HTML，无需在 Windows 安装 Studio、Python 或配置嵌入 API Key。

## 下载和校验

1. 在 [VKS connector packages](https://github.com/a3384379/VKS/actions/workflows/vks-connector.yaml) 中打开对应提交的成功运行，登录 GitHub 后在 **Artifacts** 下载 **vks-connector-windows-x64**。
2. 解压外层下载包。将其中的 `vks-plugin-3.1.0-rc.7-windows-x64.zip` 和同名的 `.zip.sha256` 文件放在同一目录。打开 Windows PowerShell 并进入这个目录。
3. 运行下方命令校验并解压。安装目录按版本分开，升级时可以保留原来的解压目录。

```powershell
$archive = 'vks-plugin-3.1.0-rc.7-windows-x64.zip'
$expected = ((Get-Content -LiteralPath "$archive.sha256" -Raw).Trim() -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw 'VKS package SHA-256 mismatch' }

$market = Join-Path $env:LOCALAPPDATA 'VKS\codex-marketplace-rc7'
$plugin = Join-Path $market 'plugins\vks'
if (Test-Path -LiteralPath $plugin) { throw "VKS package folder already exists: $plugin" }
Expand-Archive -LiteralPath $archive -DestinationPath $plugin
Get-Content -LiteralPath (Join-Path $plugin 'build-info.json')
```

在 `build-info.json` 核对 `platform` 为 `windows-x64`，`commit` 对应 Actions 中的提交。候选包的 Windows 程序尚未做 Authenticode 签名。

## 加入 Codex 本地市场

在同一个 PowerShell 窗口运行。命令使用 Windows 版 Codex CLI。若 `codex --version` 找不到命令，先按官方文档的 Windows 选项安装 CLI，并重新打开 PowerShell。另开窗口后重新设置上一步的 `$market`。 [Codex CLI 安装文档](https://developers.openai.com/codex/cli/)

```powershell
$catalog = Join-Path $market '.agents\plugins'
New-Item -ItemType Directory -Force -Path $catalog | Out-Null
$manifest = @'
{
  "name": "vks-local",
  "interface": { "displayName": "VKS Local" },
  "plugins": [{
    "name": "vks",
    "source": { "source": "local", "path": "./plugins/vks" },
    "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
    "category": "Productivity"
  }]
}
'@
[IO.File]::WriteAllText((Join-Path $catalog 'marketplace.json'), $manifest, [Text.UTF8Encoding]::new($false))
codex plugin marketplace add "$market"
codex plugin add vks@vks-local
codex plugin list --marketplace vks-local --json
```

完全退出并重新打开 ChatGPT 桌面应用。在 Codex 的「插件」中确认 VKS 已启用，然后点左栏的 VKS。若电脑上已经安装过旧版 VKS，先用 `codex plugin marketplace list` 确认 `vks-local` 指向哪个目录。更新时可在 Codex 卸载旧插件，再用 `codex plugin marketplace remove vks-local` 移除旧目录的登记，然后执行上面的两个 `add` 命令。 [Codex 本地市场文档](https://developers.openai.com/plugins/build/plugins#add-a-marketplace-from-the-cli)

## 选择成品库和测试

1. 把你自己的成品库 HTML 复制到 Windows 本地磁盘。在 VKS 左栏点「选择成品库」或「更换成品库」，选中文件并核对项目名和保存版本。同一个 HTML 包含导出时的原文、已有的关联和检索数据。
2. 搜两个不同主题。每次都点结果中的「在成品库打开原文章节」，确认系统浏览器进入该文件的「知识文档」，并定位到结果中显示的章节。浏览器地址应包含 `#section=`，两次点击的章节值应不同。
3. 用支持本地向量的 HTML 做一次换说法查询。结果应显示「本地向量 + 关键词」。旧版 HTML 缺少本地向量时仍可按关键词查找。
4. 在一个新 Codex 对话中也可以输入「用 VKS 查询当前成品库，找到与我的问题对应的规则，并返回原文章节引用。找不到依据时请注明。」对话里附上你要查的具体问题。

文件选择器打不开时，可从解压目录直接指定文件。执行后重新打开 VKS 左栏。

```powershell
$plugin = Join-Path $env:LOCALAPPDATA 'VKS\codex-marketplace-rc7\plugins\vks'
& (Join-Path $plugin 'bin\vks-connect.exe') library use 'C:\VKS\我的成品库.html'
& (Join-Path $plugin 'bin\vks-connect.exe') library status
```
