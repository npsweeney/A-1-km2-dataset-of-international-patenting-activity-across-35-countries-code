# Shared figure export helpers
options(bitmapType = "cairo")
# Apply the common theme and export each requested image format

save_paper_figure <- function(
    plot,
    filename,
    width = 6.5,
    height = 4,
    dpi = 320,
    formats = "png",
    bg = "white") {
  dir.create(dirname(filename), recursive = TRUE, showWarnings = FALSE)
  base <- tools::file_path_sans_ext(filename)
  styled_plot <- plot + (theme_paper() + plot$theme)

  for (fmt in formats) {
    ggplot2::ggsave(
      filename = paste0(base, ".", fmt),
      plot = styled_plot,
      width = width,
      height = height,
      dpi = dpi,
      bg = bg
    )
  }

  invisible(styled_plot)
}
