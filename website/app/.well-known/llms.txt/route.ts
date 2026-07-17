import { markdownResponse } from '@/lib/llm-response';
import { getLLMsIndex } from '@/lib/source';

export const revalidate = false;

export function GET() {
  return markdownResponse(getLLMsIndex());
}
