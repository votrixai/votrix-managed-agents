import { markdownResponse } from '@/lib/llm-response';
import { getLLMsFullText } from '@/lib/source';

export const revalidate = false;

export async function GET() {
  return markdownResponse(await getLLMsFullText());
}
