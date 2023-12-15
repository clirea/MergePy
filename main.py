import os

def create_combined_python_file(output_filename):
    # Get all .py files in the current directory
    py_files = [f for f in os.listdir('.') if f.endswith('.py')]

    # Read the contents of each file and combine them into a single string
    combined_content = ""
    for file in py_files:
        with open(file, 'r', encoding='utf-8') as f:
            content = f.read()
            combined_content += f"```{file}\n{content}\n```\n"

    # Save the combined content into a new file
    with open(output_filename, 'w', encoding='utf-8') as f:
        f.write(combined_content)

    return output_filename

# Create the combined Python file
output_filename = 'Pythonファイル内容全文.txt'
create_combined_python_file(output_filename)