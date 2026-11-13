```
# 导出
pnpm dlx wrangler@4.161.0 d1 export emdash-site --remote --compatibility=sqlite3_cli_only --output=data/emdash-source-full.sql
# 转换sql
python3 data/convert_d1_dump.py data/emdash-source-full.sql data/emdash-target.sql
# 创建新库
pnpm dlx wrangler@4.161.0 d1 create emdash-site-copy  --no-update-config
# 上传
pnpm dlx wrangler@4.161.0 d1 execute emdash-site-copy --remote --file=data/emdash-target-ordered.sql
```
