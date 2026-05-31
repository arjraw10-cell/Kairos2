from kairos.config import Config
from kairos.agent import Agent
from kairos.cli import CLI
import os
import sys

def main():
    # Validate config
    try:
        Config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please create a .env file with your OpenAI API key.")
        print("See .env.example for reference.")
        sys.exit(1)

    # Get workspace from command line or use current directory
    workspace = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    
    if not os.path.isdir(workspace):
        print(f"Error: Workspace '{workspace}' is not a valid directory")
        sys.exit(1)

    # Initialize
    cli = CLI()
    agent = Agent(workspace)
    
    cli.print_banner()
    cli.print_workspace(workspace)
    cli.print_info("Type your request or 'exit' to quit")
    cli.print_info("Use 'clear' to clear the screen")
    cli.print_info("Use 'reset' to reset conversation history")
    cli.print()

    while True:
        user_input = cli.get_user_input()
        
        if user_input is None:
            break
        
        if user_input.lower() in ('exit', 'quit', 'q'):
            break
        
        if user_input.lower() == 'clear':
            cli.clear_screen()
            cli.print_banner()
            cli.print_workspace(workspace)
            continue
        
        if user_input.lower() == 'reset':
            agent.reset()
            cli.print_info("Conversation history reset")
            continue
        
        if not user_input.strip():
            continue

        # Process request
        cli.print_thinking()
        
        try:
            response = agent.run(user_input)
            cli.print_response(response)
        except Exception as e:
            cli.print_error(f"Agent error: {str(e)}")

    cli.print_exit()

if __name__ == "__main__":
    main()