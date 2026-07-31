import { describe, expect, it } from "vitest";
import { commitUrl, getBuildSha } from "./buildInfo.ts";

const REPO_URL = "https://github.com/2AMLogic/klayout-tools";

describe("getBuildSha", () => {
  it("returns the trimmed git output when git succeeds", () => {
    const result = getBuildSha({}, () => "abc1234\n" as never);
    expect(result).toEqual({ sha: "abc1234", fromGit: true });
  });

  it("falls back to GITHUB_SHA (sliced to 7 chars) when git throws", () => {
    const throwingExec = () => {
      throw new Error("not a git repository");
    };
    const result = getBuildSha(
      { GITHUB_SHA: "0123456789abcdef0123456789abcdef01234567" },
      throwingExec as never,
    );
    expect(result).toEqual({ sha: "0123456", fromGit: false });
  });

  it("falls back to \"unknown\" when git throws and GITHUB_SHA is unset", () => {
    const throwingExec = () => {
      throw new Error("git: command not found");
    };
    const result = getBuildSha({}, throwingExec as never);
    expect(result).toEqual({ sha: "unknown", fromGit: false });
  });

  it("falls back to \"unknown\" when git returns an empty string", () => {
    const result = getBuildSha({}, () => "" as never);
    expect(result).toEqual({ sha: "unknown", fromGit: false });
  });
});

describe("commitUrl", () => {
  it("appends /commit/<sha> for a resolved sha", () => {
    expect(commitUrl(REPO_URL, { sha: "abc1234", fromGit: true })).toBe(
      `${REPO_URL}/commit/abc1234`,
    );
  });

  it("falls back to the bare repo URL when the sha is unknown", () => {
    expect(commitUrl(REPO_URL, { sha: "unknown", fromGit: false })).toBe(REPO_URL);
  });
});
