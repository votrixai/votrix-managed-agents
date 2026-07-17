import { routeRequest } from "./router";

export default {
  async fetch(request, env): Promise<Response> {
    return routeRequest(request, env);
  },
} satisfies ExportedHandler<Env>;
