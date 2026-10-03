from harness.agent import Session, run

if __name__ == "__main__":
    print("🤖 File Agent (type 'quit' to exit)")
    session = Session()
    print(f"Workspace: {session.workspace}")
    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in ("quit", "exit"):
            break
        response = run(user_input, session)
        print(f"\nAgent: {response}")
