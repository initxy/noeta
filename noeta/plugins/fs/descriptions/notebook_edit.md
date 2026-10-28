Changes one cell of a Jupyter notebook (`.ipynb`): replaces its source, adds a new cell, or removes one.

- The notebook must have been read with Read in this conversation; otherwise the edit is refused.
- Give `notebook_path` as an absolute path.
- Cells are named by the id Read prints in `<cell id="...">`; replacing or deleting needs `cell_id`.
- Without `edit_mode` the cell is replaced. With `insert` the new cell lands after the cell `cell_id` names, or first when there is no `cell_id`, and `cell_type` must be set. With `delete` the cell is removed.
