import path from "node:path";

export interface Config {
  catalogOutput: string;
  rawOutput: string;
}

export function getConfig(): Config {
  const root = path.resolve(process.cwd());
  return {
    catalogOutput: path.join(root, "db", "seed", "catalog.json"),
    rawOutput: path.join(root, "data", "demo", "raw-products.json")
  };
}
