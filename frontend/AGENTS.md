# UI 样式约定

- 所有页面样式只放在 `frontend/styles.css`，由 `src/main.tsx` 导入一次。
- 组件复用全局样式类与 `data-*` 状态；禁止内联 `style`、DOM `style` 操作、组件样式表及运行时 CSS 注入。
- 字号、行高、间距、颜色、控件尺寸使用全局设计变量。新页面优先复用按钮、字段、表格、面板等现有样式。
- 正文与常用控件使用 14–16px；辅助文字至少 12px；同一行的文字与控件垂直居中。
- 批量裁决确认栏位于列表末尾，使用普通文档流，不得悬浮跟随。
- 动态数据使用原生控件属性、SVG 图形几何属性或 `data-*`，不生成 CSS。
- `src/cover.ts` 的 Canvas 绘图是封面导出规格，不属于页面样式；调整 UI 时不得改动已确认的封面排版。
- 时间显示统一调用 `src/dateTime.ts`：时间戳为 `YYYY-MM-DD HH:mm`（Asia/Shanghai），纯日期为 `YYYY-MM-DD`，不得省略年份或对纯日期作时区换算。
- 提交前运行 `npm run check:styles` 和 `npm run build`；涉及布局时检查桌面及手机页面。
