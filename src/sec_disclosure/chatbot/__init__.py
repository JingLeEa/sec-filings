"""Natural-language chatbot over the SEC 10-K disclosure-change database.

    from sec_disclosure.chatbot import Chatbot
    reply = Chatbot().ask("How many new risk factors did Intel add in 2025?")
    print(reply.answer)

See docs/chatbot.md for setup (docker compose up -> load fixtures -> run).
"""
from .engine import Chatbot, ChatResponse

__all__ = ["Chatbot", "ChatResponse"]
