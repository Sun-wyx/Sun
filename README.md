# 租房位置决策小应用 v2

这是一个可直接部署到 Render 的租房位置比较应用。新版不再要求用户填写百分制权重，也不再让用户给安全感、生活配套打 0–100 分。

## 新版决策方式

1. 月租预算和可接受通勤时间作为硬条件。
2. 用户只选择一种决策侧重：均衡、通勤优先、租金优先或居住体验优先。
3. 居住环境和生活配套使用定性选项，例如“比较满意”“可以接受”“还需实地核实”。
4. 结果显示“优先看房、先核实再决定、可作为备选、不建议优先”，不显示总分。
5. 排序内部只使用规则和先后关系，不把定性信息伪装成精确测量值。

## API

- OpenStreetMap Nominatim：地址解析。
- OSRM：预计驾车距离和通勤时间。
- OpenAI 兼容接口：可选生成文字建议。AI 接口失败时，地图和规则排序仍然正常返回。

## 修复的报错

旧版在第三方接口返回空内容时，会直接显示：

```text
Expecting value: line 1 column 1 (char 0)
```

新版会区分地址解析、路线和 AI 接口错误，并返回可读提示；前端也不再直接假定所有响应都是 JSON。AI 接口是可选项，即使配置错误也不会阻断核心分析。

## 本地运行

```bash
python app.py
```

浏览器打开 `http://127.0.0.1:8000`。

## Render 更新部署

将新版文件覆盖 GitHub 仓库后提交并推送。Render 通常会自动重新部署；也可以在 Render 的 Deploy 页面选择 **Deploy latest commit**。

建议环境变量：

```text
LLM_API_KEY=重新生成的密钥
LLM_BASE_URL=https://你的兼容接口域名/v1
LLM_MODEL=模型名称
APP_USER_AGENT=rent-decision-app/2.0 your-email@example.com
```

若暂时不需要 AI 建议，可以删除三个 `LLM_` 环境变量，地图和排序仍可使用。

## 安全

已经在聊天、截图或仓库中出现过的密钥应立即吊销并重新生成。密钥只能保存在 Render Environment 中，不能写入前端 JavaScript、HTML 或 Git 仓库。
