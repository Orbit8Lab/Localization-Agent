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
    lqa.data$label <- sub('^L[0-9]+[a-z]?_', '', lqa.data$condition);

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
        main.cex = 1.1,
        xlab.label = 'Workflow condition',
        ylab.label = 'Rate',
        xaxis.cex = 0.9,
        yaxis.cex = 0.9,
        xlab.cex = 1.1,
        ylab.cex = 1.1,
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
    fp.data$label <- sub('^L[0-9]+[a-z]?_', '', fp.data$condition);
    fp.data$label <- factor(fp.data$label, levels = unique(fp.data$label));

    # Stack order must be an explicit factor and the legend built from
    # THOSE levels. Letting create.barplot infer groups from a character
    # vector while labelling the key with sort(unique(...)) produced a
    # legend whose colours did not match the stack -- the figure looked
    # finished and was wrong.
    bug.levels <- sort(unique(fp.data$bug_type));
    fp.data$bug_type <- factor(fp.data$bug_type, levels = bug.levels);
    bug.colours <- default.colours(length(bug.levels));

    create.barplot(
        formula = n ~ label,
        data = fp.data,
        groups = fp.data$bug_type,
        stack = TRUE,
        filename = file.path(fig.dir, 'fig4b_fp_composition.png'),
        main = 'False positives by check type',
        main.cex = 1.1,
        xlab.label = 'Workflow condition',
        ylab.label = 'False positives',
        xaxis.cex = 0.9,
        yaxis.cex = 0.9,
        xlab.cex = 1.1,
        ylab.cex = 1.1,
        xaxis.rot = 30,
        col = bug.colours,
        # Headroom so the key never sits on top of a bar.
        ylimits = c(0, max(tapply(fp.data$n, fp.data$label, sum)) * 1.45),
        legend = list(
            inside = list(
                fun = draw.key,
                args = list(key = list(
                    points = list(col = 'black', pch = 22, cex = 1.5,
                                  fill = bug.colours),
                    text = list(lab = bug.levels),
                    padding.text = 2,
                    columns = 2
                    )),
                x = 0.02, y = 0.97
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
    tr.data$label <- sub('^B[0-9]+[a-z]?_', '', tr.data$condition);
    tr.data$label <- factor(tr.data$label, levels = tr.data$label);

    create.barplot(
        formula = violation_rate ~ label,
        data = tr.data,
        filename = file.path(fig.dir, 'fig5_translation_defects.png'),
        main = 'Style-rule violation rate by workflow condition',
        main.cex = 1.1,
        xlab.label = 'Workflow condition',
        ylab.label = 'Fraction of strings violating a client rule',
        xaxis.cex = 0.9,
        yaxis.cex = 0.9,
        xlab.cex = 1.1,
        ylab.cex = 1.1,
        xaxis.rot = 30,
        col = default.colours(1),
        resolution = resolution,
        width = 6,
        height = 5
        );

    # Cost vs quality: the §6 operational argument. Plotted against
    # TOKENS rather than calls -- several conditions share a call count
    # (B2/B3/B4 are all ~9-10 batches), which collapses the x-axis, and
    # a log scale over 2-3 tied points produced an invalid viewport.
    # Tokens are also the quantity that maps to spend.
    #
    # Terminology error count is the y-axis here, not the style-rule
    # rate: it is where the glossary effect lives (54 -> 0), and it is
    # the axis on which the conditions actually separate.
    tr.data$tokens.k <- tr.data$tokens / 1000;
    create.scatterplot(
        formula = term_errors ~ tokens.k,
        data = tr.data,
        filename = file.path(fig.dir, 'fig6_cost_quality.png'),
        main = 'Cost vs. terminology accuracy',
        main.cex = 1.1,
        xlab.label = 'Tokens spent (thousands)',
        ylab.label = 'Locked-term errors',
        xaxis.cex = 0.9,
        yaxis.cex = 0.9,
        xlab.cex = 1.1,
        ylab.cex = 1.1,
        cex = 1.8,
        pch = 19,
        col = default.colours(nrow(tr.data)),
        # Label each point: with 3-4 conditions a legend costs more
        # space than it saves.
        add.text = TRUE,
        text.labels = sub('^B[0-9]+[a-z]?_', '', tr.data$condition),
        text.x = tr.data$tokens.k,
        text.y = tr.data$term_errors + max(tr.data$term_errors) * 0.07,
        text.cex = 0.85,
        ylimits = c(-3, max(tr.data$term_errors) * 1.2 + 3),
        resolution = resolution,
        width = 6,
        height = 5
        );
    }

cat('figures written to', normalizePath(fig.dir), '\n');
