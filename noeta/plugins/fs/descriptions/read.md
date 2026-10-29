Reads one file from the local filesystem: text, an image, a PDF or a Jupyter notebook.

- Give `file_path` as an absolute path.
- Without `limit` you get at most 2000 lines from the start of the file. When you already know which part you need, read only that part; this matters most for large files.
- Lines come back numbered like `cat -n`, starting at 1.
- Images (PNG, JPG and the like) are shown to you as images.
- PDFs are read through `pages`, at most 20 pages per call; a PDF longer than 10 pages needs `pages`.
- Notebooks (`.ipynb`) come back as their cells, with their outputs.
- A directory, a path that does not exist or an empty file gives an error or a note instead of content.
- Do not read a file again just to check an edit you made: a failed Edit or Write reports an error, and the file's state is tracked for you.
