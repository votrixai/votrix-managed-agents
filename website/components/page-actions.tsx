import {
  MarkdownCopyButton,
  ViewOptionsPopover,
} from 'fumadocs-ui/layouts/docs/page';

interface PageActionsProps {
  markdownUrl: string;
}

export function PageActions({ markdownUrl }: PageActionsProps) {
  return (
    <div className="flex flex-row items-center gap-2 border-b pb-6">
      <MarkdownCopyButton markdownUrl={markdownUrl} />
      <ViewOptionsPopover markdownUrl={markdownUrl} />
    </div>
  );
}
