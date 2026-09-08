### plot_figures.R #############################################################
# Publication figures for the IAAI-27 submission.
#
# The paper is not double-blind, so these render as the submitted artwork.
# Reads the tidy CSVs written by make_figures.py; no experiment is re-run
# here, so a figure tweak never costs API spend.

library(BoutrosLab.plotting.general);

### PATHS #####################################################################
script.dir <- dirname(normalizePath(sub('^--file=', '', grep('^--file=', commandArgs(), value = TRUE))[1]));
if (is.na(script.dir) || !nzchar(script.dir)) script.dir <- '.';
fig.dir <- file.path(script.dir, '..', 'figures');
resolution <- 300;

### FIGURE 4a — LQA accuracy by condition #####################################
# Recall and precision side by side: the point of the ablation is that the
# glossary layer lifts recall WITHOUT paying for it in false positives, and
# a single F1 bar would hide exactly that.
lqa.file <- file.path(fig.dir, 'fig_lqa_accuracy.csv');
if (file.exists(lqa.file)) {
    lqa.data <- read.csv(lqa.file, stringsAsFactors = FALSE);
    lqa.data$label <- sub('^L[0-9]_', '', lqa.data$condition);

    plot.data <- data.frame(
        condition = rep(lqa.data$label, 2),
        metric    = rep(c('Precision', 'Recall'), each = nrow(lqa.data)),
        value     = c(lqa.data$precision, lqa.data$recall)
        );
    plot.data$condition <- factor(plot.data$condition, levels = lqa.data$label);

    create.barplot(
        formula = value ~ condition,
        data = plot.data,
        groups = plot.data$metric,
        filename = file.path(fig.dir, 'fig4a_lqa_accuracy.png'),
        main = 'LQA detection accuracy vs. human post-editor',
        main.cex = 1.3,
        xlab.label = 'Workflow condition',
        ylab.label = 'Rate',
        xaxis.cex = 1.0,
        yaxis.cex = 1.0,
        xaxis.rot = 30,
        ylimits = c(0, 1),
        yat = seq(0, 1, 0.2),
        col = default.colours(2),
        legend = list(
            inside = list(
                fun = draw.key,
                args = list(key = list(
                    points = list(col = 'black', pch = 22, cex = 1.5,
                                  fill = default.colours(2)),
                    text = list(lab = c('Precision', 'Recall')),
                    padding.text = 2
                    )),
                x = 0.04, y = 0.95
                )
            ),
        resolution = resolution,
        width = 7,
        height = 5
        );
    }

### FIGURE 4b — false-positive composition ####################################
# Supports the §7.3 limitation: the FP mass is one miscalibrated check,
# not diffuse noise, so it is actionable rather than a cost of doing
# business.
fp.file <- file.path(fig.dir, 'fig_fp_composition.csv');
if (file.exists(fp.file)) {
    fp.data <- read.csv(fp.file, stringsAsFactors = FALSE);
    fp.data$label <- sub('^L[0-9]_', '', fp.data$condition);
    fp.data$label <- factor(fp.data$label, levels = unique(fp.data$label));

    create.barplot(
        formula = n ~ label,
        data = fp.data,
        groups = fp.data$bug_type,
        stack = TRUE,
        filename = file.path(fig.dir, 'fig4b_fp_composition.png'),
        main = 'False positives by check type',
        main.cex = 1.3,
        xlab.label = 'Workflow condition',
        ylab.label = 'False positives',
        xaxis.cex = 1.0,
        yaxis.cex = 1.0,
        xaxis.rot = 30,
        col = default.colours(length(unique(fp.data$bug_type))),
        legend = list(
            inside = list(
                fun = draw.key,
                args = list(key = list(
                    points = list(col = 'black', pch = 22, cex = 1.5,
                                  fill = default.colours(length(unique(fp.data$bug_type)))),
                    text = list(lab = sort(unique(fp.data$bug_type))),
                    padding.text = 2
                    )),
                x = 0.6, y = 0.95
                )
            ),
        resolution = resolution,
        width = 7,
        height = 5
        );
    }

### FIGURE 5 — translation defect rate by condition ###########################
# The ablation ladder: each bar adds exactly one mechanism, so the
# decrement between adjacent bars is that mechanism's contribution.
tr.file <- file.path(fig.dir, 'fig_translation_quality.csv');
if (file.exists(tr.file)) {
    tr.data <- read.csv(tr.file, stringsAsFactors = FALSE);
    tr.data$label <- sub('^B[0-9]_', '', tr.data$condition);
    tr.data$label <- factor(tr.data$label, levels = tr.data$label);

    create.barplot(
        formula = violation_rate ~ label,
        data = tr.data,
        filename = file.path(fig.dir, 'fig5_translation_defects.png'),
        main = 'Style-rule violation rate by workflow condition',
        main.cex = 1.3,
        xlab.label = 'Workflow condition',
        ylab.label = 'Fraction of strings violating a client rule',
        xaxis.cex = 1.0,
        yaxis.cex = 1.0,
        xaxis.rot = 30,
        col = default.colours(1),
        resolution = resolution,
        width = 6,
        height = 5
        );

    # Cost vs quality: the §6 operational argument. Batching cuts calls by
    # an order of magnitude, so the interesting question is what that buys
    # or costs in defects, which a scatter makes legible at a glance.
    create.scatterplot(
        formula = violation_rate ~ calls,
        data = tr.data,
        filename = file.path(fig.dir, 'fig6_cost_quality.png'),
        main = 'Cost vs. quality across conditions',
        main.cex = 1.3,
        xlab.label = 'LLM calls (log scale)',
        ylab.label = 'Style-rule violation rate',
        xaxis.cex = 1.0,
        yaxis.cex = 1.0,
        xaxis.log = TRUE,
        cex = 1.6,
        col = default.colours(nrow(tr.data)),
        resolution = resolution,
        width = 6,
        height = 5
        );
    }

cat('figures written to', normalizePath(fig.dir), '\n');
