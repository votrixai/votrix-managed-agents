import { readFile } from "node:fs/promises";

const expectedHostnames = {
  staging: "staging-vma.votrixai.com",
  production: "vma.votrixai.com",
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
  const vars = config.env?.[environment]?.vars;

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
