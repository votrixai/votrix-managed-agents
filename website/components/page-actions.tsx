import {
  MarkdownCopyButton,
  ViewOptionsPopover,
} from 'fumadocs-ui/layouts/docs/page';

interface PageActionsProps {
  markdownUrl: string;
  githubUrl?: string;
}

export function PageActions({ markdownUrl, githubUrl }: PageActionsProps) {
  return (
    <div className="flex flex-row items-center gap-2 border-b pb-6">
      <MarkdownCopyButton markdownUrl={markdownUrl} />
      <ViewOptionsPopover markdownUrl={markdownUrl} githubUrl={githubUrl} />
    </div>
  );
}
