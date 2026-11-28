# Extract the Guide helpers used by the work-plan renderer and verifier.
# Top-level functions close with a brace in column one.
/^(open_pr_linkage_rows|open_pr_annotation|approved_hold_suffix)\(\) \{/ {
    in_helper = 1
    found[$1] = 1
}
in_helper { print }
/^\}/ { in_helper = 0 }
END {
    if (!found["open_pr_linkage_rows()"] || !found["open_pr_annotation()"] || !found["approved_hold_suffix()"]) {
        print "missing work-plan helper in " FILENAME > "/dev/stderr"
        exit 1
    }
}
