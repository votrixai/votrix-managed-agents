import { copyFile, mkdir, readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const outputRoot = path.resolve(process.cwd(), 'out');
const legacyRoot = path.join(outputRoot, 'llms.mdx', 'docs');
const docsRoot = path.join(outputRoot, 'docs');

async function findMarkdownPages(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const pages = [];

  for (const entry of entries) {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) pages.push(...(await findMarkdownPages(entryPath)));
    else if (entry.isFile() && entry.name === 'content.md') pages.push(entryPath);
  }

  return pages;
}

const legacyPages = await findMarkdownPages(legacyRoot);
if (legacyPages.length === 0) {
  throw new Error(`No generated Markdown pages found under ${legacyRoot}`);
}

for (const sourcePath of legacyPages) {
  const relativeDirectory = path.relative(legacyRoot, path.dirname(sourcePath));
  const destinationPath = relativeDirectory
    ? path.join(docsRoot, `${relativeDirectory}.md`)
    : path.join(docsRoot, 'index.md');

  await mkdir(path.dirname(destinationPath), { recursive: true });
  await copyFile(sourcePath, destinationPath);

  if (relativeDirectory.startsWith(`api${path.sep}`)) {
    const markdown = await readFile(sourcePath, 'utf8');
    const requiredSections = [
      /^## (DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT|TRACE) \//m,
      /^### Responses$/m,
      /Complete OpenAPI 3\.1 schema:/,
    ];
    if (requiredSections.some((pattern) => !pattern.test(markdown))) {
      throw new Error(`Generated API Markdown is incomplete: ${sourcePath}`);
    }
  }
}

console.log(`Materialized ${legacyPages.length} canonical Markdown pages in out/docs.`);
