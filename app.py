import os
import uuid
import gradio as gr
from copilot_agents.graph import build_graph, chat

# ── Build the agent once at startup ──────────────────────────────────────────
agent = build_graph()


# ── Core respond function ─────────────────────────────────────────────────────
def respond(user_message: str, history: list, thread_id: str) -> tuple:
    """
    Called by Gradio on every user submission.

    Args:
        user_message: The new user input.
        history:      Gradio 6 messages list: [{"role": ..., "content": ...}, ...]
        thread_id:    Per-session UUID that keeps LangGraph memory isolated.

    Returns:
        ("", updated_history, thread_id)
    """
    if not user_message.strip():
        return "", history, thread_id

    # Ensure this session has a thread ID
    if not thread_id:
        thread_id = str(uuid.uuid4())

    # Call the LangGraph agent
    reply = chat(agent, thread_id, user_message)

    # Gradio 6 requires OpenAI-style message dicts (not tuples)
    history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return "", history, thread_id


# ── New-session reset ─────────────────────────────────────────────────────────
def new_session():
    """Clear history and assign a fresh thread ID (resets agent memory)."""
    return [], str(uuid.uuid4())


# ── Gradio UI ─────────────────────────────────────────────────────────────────
_theme = gr.themes.Default(
    primary_hue="violet",
    secondary_hue="indigo",
    neutral_hue="slate",
    font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui"],
)

_css = """
    /* ── Layout ── */
    #app-col { max-width: 860px; margin: 0 auto; }

    /* ── Header ── */
    #header {
        background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
        border-radius: 16px;
        padding: 28px 32px 22px;
        margin-bottom: 18px;
        box-shadow: 0 4px 24px rgba(79,70,229,0.25);
    }
    #header h1 {
        color: #fff;
        font-size: 1.75rem;
        font-weight: 700;
        margin: 0 0 6px;
        letter-spacing: -0.5px;
    }
    #header p {
        color: rgba(255,255,255,0.80);
        font-size: 0.92rem;
        margin: 0;
    }

    /* ── Chatbot bubble area ── */
    #chatbot {
        border-radius: 14px;
        border: 1px solid rgba(99,102,241,0.18);
        box-shadow: 0 2px 16px rgba(0,0,0,0.06);
        min-height: 480px;
    }

    /* ── Input row ── */
    #input-row { margin-top: 10px; gap: 8px; }
    #msg-box textarea {
        border-radius: 12px !important;
        font-size: 0.95rem !important;
    }
    #send-btn {
        border-radius: 12px !important;
        font-size: 0.95rem !important;
        font-weight: 600 !important;
        min-width: 90px;
        background: linear-gradient(135deg, #4f46e5, #7c3aed) !important;
        color: #fff !important;
        border: none !important;
    }
    #send-btn:hover { opacity: 0.88; }

    /* ── Footer row ── */
    #footer-row { margin-top: 6px; align-items: center; gap: 10px; }
    #new-session-btn {
        border-radius: 10px !important;
        font-size: 0.85rem !important;
        color: #6366f1 !important;
        border: 1px solid rgba(99,102,241,0.35) !important;
        background: transparent !important;
    }
    #new-session-btn:hover {
        background: rgba(99,102,241,0.07) !important;
    }
    #thread-display {
        font-size: 0.74rem;
        color: #94a3b8;
        font-family: monospace;
        text-align: right;
    }
    """

with gr.Blocks(title="Chandra — AWS Cloud Watcher") as demo:

    # ── State: per-tab thread ID ──────────────────────────────────────────────
    thread_id_state = gr.State(str(uuid.uuid4()))

    with gr.Column(elem_id="app-col"):

        # Header
        gr.HTML("""
        <div id="header">
            <h1>☁️ Chandra — AWS Cloud Watcher</h1>
            <p>AI-powered monitoring agent · CloudWatch · X-Ray · CloudTrail · Config · Cost Explorer</p>
        </div>
        """)

        # Chat area
        chatbot = gr.Chatbot(
            label="Conversation",
            elem_id="chatbot",
            buttons=["copy", "copy_all"],
            avatar_images=(
                None,                          # user  – default
                "https://api.dicebear.com/7.x/bottts-neutral/svg?seed=chandra&backgroundColor=4f46e5",
            ),
            render_markdown=True,
            layout="bubble",
            height=480,
        )

        # Input row
        with gr.Row(elem_id="input-row"):
            msg_box = gr.Textbox(
                placeholder="Ask about CPU, X-Ray traces, costs, CloudTrail events…",
                show_label=False,
                lines=1,
                max_lines=6,
                elem_id="msg-box",
                scale=9,
                autofocus=True,
            )
            send_btn = gr.Button("Send ➤", elem_id="send-btn", scale=1, variant="primary")

        # Footer row
        with gr.Row(elem_id="footer-row"):
            new_btn = gr.Button("🔄 New Session", elem_id="new-session-btn", scale=0)
            thread_display = gr.Markdown("", elem_id="thread-display")

        # ── Example prompts ───────────────────────────────────────────────────
        gr.Examples(
            examples=[
                "Get CPU utilization for instance i-0327bf109dc40d412 over the last 6 hours",
                "Show me recent X-Ray traces from the last 2 hours",
                "What AWS resources were recently changed or deleted?",
                "List recent CloudTrail API events from the last hour",
                "Give me a cost breakdown for the past 7 days by service",
            ],
            inputs=msg_box,
            label="💡 Example prompts",
        )

    # ── Wire up events ────────────────────────────────────────────────────────

    # Submit on Enter or Send button
    submit_args = dict(
        fn=respond,
        inputs=[msg_box, chatbot, thread_id_state],
        outputs=[msg_box, chatbot, thread_id_state],
    )
    msg_box.submit(**submit_args)
    send_btn.click(**submit_args)

    # New session — clears history + rotates thread ID
    new_btn.click(
        fn=new_session,
        inputs=[],
        outputs=[chatbot, thread_id_state],
    )

    # Show current thread ID in footer whenever it changes
    thread_id_state.change(
        fn=lambda tid: f"🔑 Session: `{tid}`",
        inputs=[thread_id_state],
        outputs=[thread_display],
    )

    # Show initial thread ID on load
    demo.load(
        fn=lambda tid: f"🔑 Session: `{tid}`",
        inputs=[thread_id_state],
        outputs=[thread_display],
    )


if __name__ == "__main__":
    _host = os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0")
    _port = int(os.environ.get("GRADIO_SERVER_PORT", "7861"))
    _in_container = os.environ.get("GRADIO_SERVER_NAME") == "0.0.0.0"

    demo.launch(
        server_name=_host,
        server_port=_port,
        share=False,
        show_error=True,
        inbrowser=not _in_container,   # open browser locally, skip in Docker
        theme=_theme,
        css=_css,
    )
