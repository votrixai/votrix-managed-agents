import { readFile } from "node:fs/promises";

const expectedHostnames = {
  staging: "staging-api.vma.votrixai.com",
  production: "api.vma.votrixai.com",
};

function fail(message) {
  console.error(`Deployment blocked: ${message}`);
  process.exitCode = 1;
}

const environment = process.argv[2];
if (!(environment in expectedHostnames)) {
  fail("expected environment to be staging or production");
} else {
  const rawConfig = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  const config = JSON.parse(rawConfig);
  const environmentConfig = config.env?.[environment];
  const vars = environmentConfig?.vars;
  const routes = environmentConfig?.routes;

  const hasExpectedRoute =
    Array.isArray(routes) &&
    routes.length === 1 &&
    routes[0]?.pattern === expectedHostnames[environment] &&
    routes[0]?.custom_domain === true &&
    Object.keys(routes[0]).length === 2;

  if (!hasExpectedRoute) {
    fail(
      `routes must contain exactly one Custom Domain for ${expectedHostnames[environment]}`,
    );
  }

  if (vars?.PUBLIC_HOSTNAME !== expectedHostnames[environment]) {
    fail(`PUBLIC_HOSTNAME must be ${expectedHostnames[environment]}`);
  } else {
    let origin;
    try {
      origin = new URL(vars.ORIGIN_URL);
    } catch {
      fail("ORIGIN_URL is not a valid URL");
    }

    if (origin !== undefined) {
      const deployable =
        origin.protocol === "https:" &&
        origin.hostname.endsWith(".run.app") &&
        origin.hostname !== "run.app" &&
        origin.port === "" &&
        origin.pathname === "/" &&
        origin.search === "" &&
        origin.hash === "" &&
        origin.username === "" &&
        origin.password === "";

      if (!deployable) {
        fail("ORIGIN_URL must be replaced with a bare https://*.run.app Cloud Run origin");
      } else {
        console.log(
          `Validated ${environment}: ${expectedHostnames[environment]} -> ${origin.origin}`,
        );
      }
    }
  }
}
