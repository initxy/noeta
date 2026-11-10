import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Api } from "../api/client";
import { Composer } from "./Composer";
import { RewindDialog } from "./Controls";

describe("dialogs", () => {
  it("Esc closes the rewind dialog, not the turn behind it, and each choice says what it does", async () => {
    const api = { rewindPoints: vi.fn().mockResolvedValue([{ index: 0, text: "fix it", files: ["a.py"] }]) };
    const onClose = vi.fn();
    const onStop = vi.fn();
    render(
      <>
        <Composer busy commands={[]} findFiles={async () => []} onSend={() => undefined} onStop={onStop} />
        <RewindDialog api={api as unknown as Api} taskId="t1" onClose={onClose} onRewound={() => undefined} />
      </>,
    );
    expect(await screen.findByRole("button", { name: /Conversation/ })).toHaveTextContent("Chat goes back, files stay");
    await userEvent.click(screen.getByLabelText("Message"));
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();
  });
});
