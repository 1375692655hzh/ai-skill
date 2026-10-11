# 腾讯文档上云与验证（tencent-docs-upload）

## 前置（一次性）

1. 安装腾讯文档 skill 并完成授权（本机 mcporter 配置 token，等同账号操作凭证，勿提交进仓库）
2. 验证通路：`mcporter call "tencent-docs" "get_content" --args '{"file_id": "<任一已有文档>"}'`

## 上云流程（build 命令已内置）

1. **建档**：`create_smartcanvas_by_mdx` `{"title": "土耳其财经快讯简报 · YYYY-MM-DD", "mdx": chunks[0]}`
   - 标题只写产出日期，**不加窗口后缀**（用户明确要求）
2. **分块追加**：`smartcanvas.edit` `{"file_id": fid, "action": "INSERT_AFTER", "content": chunks[i]}`
   - ~12000 字符/块（config `chunk_limit`），防"Argument list too long"
   - 正文从 md 第 2 行起（跳过 H1，标题已由建档承载）
3. **瞬时报错**：追加偶发 HTTP 405（限流）→ `python scripts/retry_append.py <file_id> <本地md路径>` 按同一切分补发剩余块
4. **落账**：成功后写 `tr_data/build_state.json`（prod_date/tag/file_id/url/ts）——一天一份闸的数据源

## 验证（get_content 的坑）

```
mcporter call "tencent-docs" "get_content" --args '{"file_id": "<file_id>"}'
```

- **纯文本提取对大文档会截断**：176 条的文档只回 ~159 条、分区 header 可能缺失
- 完整性判定顺序：**build 输出的条数为准** → 抽查 3-5 个关键词 → 命中文末最旧条目关键词
- 三区（🔴/🟠/⚪）标题必须在；error 字段应为空

## 常见错误码

| 现象 | 原因与处置 |
|------|-----------|
| code 12977 标题非法 | 标题含全角冒号等字符（如"（12:00起）"）——标题规则已规避 |
| Argument list too long | 未分块或块超限——检查 chunk_limit |
| HTTP 405 | 瞬时限流——retry_append.py 补发 |
| 追加后文档缺失尾部 | 分块中途失败——retry_append 后必须重新 get_content 验证 |
