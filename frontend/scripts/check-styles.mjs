import fs from 'node:fs/promises';
import path from 'node:path';
import ts from 'typescript';
import postcss from 'postcss';

const root = path.resolve(import.meta.dirname, '..');
const failures = [];
async function walk(directory) {
  const output = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    if (['node_modules', 'dist', '.git'].includes(entry.name)) continue;
    const file = path.join(directory, entry.name);
    output.push(...(entry.isDirectory() ? await walk(file) : [file]));
  }
  return output;
}
const files = await walk(root);
const styles = files.filter((file) => /\.(css|scss|sass|less)$/.test(file));
if (styles.length !== 1 || styles[0] !== path.join(root, 'styles.css'))
  failures.push(
    '页面样式只能存放在 frontend/styles.css：' +
      styles.map((file) => path.relative(root, file)).join(', '),
  );
let imports = 0;
for (const file of files.filter(
  (file) => file.startsWith(path.join(root, 'src') + path.sep) && /\.[jt]sx?$/.test(file),
)) {
  const source = ts.createSourceFile(
    file,
    await fs.readFile(file, 'utf8'),
    ts.ScriptTarget.Latest,
    true,
  );
  const fail = (node, message) =>
    failures.push(
      `${path.relative(root, file)}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1} ${message}`,
    );
  function visit(node) {
    if (
      ts.isImportDeclaration(node) &&
      ts.isStringLiteral(node.moduleSpecifier) &&
      /\.(css|scss|sass|less)$/.test(node.moduleSpecifier.text)
    ) {
      imports++;
      if (file !== path.join(root, 'src/main.tsx') || node.moduleSpecifier.text !== '../styles.css')
        fail(node, '只允许 main.tsx 导入全局样式表');
    }
    if (ts.isJsxAttribute(node) && node.name.getText(source) === 'style')
      fail(node, '禁止内联 style；使用全局样式类');
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
      node.tagName.getText(source) === 'style'
    )
      fail(node, '禁止组件内的 style 标签');
    if (ts.isPropertyAccessExpression(node) && node.name.text === 'style')
      fail(node, '禁止 DOM style；使用 classList 或 data 属性');
    if (
      ts.isElementAccessExpression(node) &&
      ts.isStringLiteral(node.argumentExpression) &&
      node.argumentExpression.text === 'style'
    )
      fail(node, '禁止通过下标访问 DOM style');
    if (
      ts.isPropertyAssignment(node) &&
      node.name.getText(source).replaceAll(/["']/g, '') === 'style'
    ) {
      const isFontDescriptor =
        ts.isObjectLiteralExpression(node.parent) &&
        ts.isNewExpression(node.parent.parent) &&
        node.parent.parent.expression.getText(source) === 'FontFace';
      if (!isFontDescriptor) fail(node, '禁止样式对象透传；使用全局样式类');
    }
    if (ts.isIdentifier(node) && node.text === 'CSSProperties')
      fail(node, '禁止组件声明内联样式类型');
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const method = node.expression.name.text;
      const first = node.arguments[0];
      if (
        ['insertRule', 'addRule', 'replaceSync'].includes(method) ||
        (['setAttribute', 'createElement'].includes(method) &&
          first &&
          ts.isStringLiteral(first) &&
          first.text === 'style')
      )
        fail(node, '禁止运行时注入样式');
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
if (imports !== 1) failures.push(`全局样式必须只导入一次，实际 ${imports} 次`);
const css = postcss.parse(await fs.readFile(path.join(root, 'styles.css'), 'utf8'));
css.walkAtRules('import', (node) =>
  failures.push(`styles.css:${node.source.start.line} 禁止导入额外样式表`),
);
css.walkDecls('font-size', (node) => {
  if (!/^var\(--font-[\w-]+\)$|^inherit$/.test(node.value))
    failures.push(`styles.css:${node.source.start.line} 字号必须引用全局 --font-* 变量`);
});
for (const file of files.filter((file) => file.endsWith('.html'))) {
  if (/<style\b|\sstyle\s*=/i.test(await fs.readFile(file, 'utf8')))
    failures.push(`${path.relative(root, file)} 禁止页面内嵌样式`);
}
if (failures.length) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else console.log('样式检查通过：单一全局样式表，无组件内联样式，字号使用全局变量。');
