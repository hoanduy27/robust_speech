import os
import sys
from speechbrain.utils import edit_distance
from jiwer import wer


def print_wer_summary(wer_details, file=sys.stdout):
    """Prints out WER summary details in human-readable format.

    This function essentially mirrors the Kaldi compute-wer output format.

    Arguments
    ---------
    wer_details : dict
        Dict of wer summary details,
        see ``speechbrain.utils.edit_distance.wer_summary``
        for format.
    file : stream
        Where to write. (default: sys.stdout)
    """
    print(
        "%WER {WER:.2f} [ {num_edits} / {num_scored_tokens}, {insertions} ins, {deletions} del, {substitutions} sub ]".format(  # noqa
            **wer_details
        ),
        file=file,
        end="",
    )
    print(
        " [PARTIAL]"
        if wer_details["num_scored_sents"] < wer_details["num_ref_sents"]
        else "",
        file=file,
    )
    print(
        "%SER {SER:.2f} [ {num_erraneous_sents} / {num_scored_sents} ]".format(
            **wer_details
        ),
        file=file,
    )
    print(
        "Scored {num_scored_sents} sentences, {num_absent_sents} not present in hyp.".format(  # noqa
            **wer_details
        ),
        file=file,
    )



def print_alignments(
    details_by_utterance,
    file=sys.stdout,
    empty_symbol="<eps>",
    separator=" ; ",
    print_header=True,
    sample_separator=None,
    total_iters = 1,
):
    """Print WER summary and alignments.

    Arguments
    ---------
    details_by_utterance : list
        List of wer details by utterance,
        see ``speechbrain.utils.edit_distance.wer_details_by_utterance``
        for format. Has to have alignments included.
    file : stream
        Where to write. (default: sys.stdout)
    empty_symbol : str
        Symbol to use when aligning to nothing.
    separator : str
        String that separates each token in the output. Note the spaces in the
        default.
    print_header: bool
        Whether to print headers
    sample_separator: str
        A separator to put between samples (optional)
    """
    for index, dets in enumerate(details_by_utterance):
        if index % total_iters == 0:
            if dets["scored"]:
                if print_header:
                    _print_alignment_header(dets, file=file)
                _print_alignment(
                    dets["alignment"],
                    dets["ref_tokens"],
                    dets["hyp_tokens"],
                    file=file,
                    empty_symbol=empty_symbol,
                    separator=separator,
                )
                if sample_separator:
                    print(sample_separator, file=file)


# The following internal functions are used to
# print out more specific things

def _print_top_wer_utts(top_non_empty, top_empty, file=sys.stdout):
    print("=" * 80, file=file)
    print("UTTERANCES WITH HIGHEST WER", file=file)
    if top_non_empty:
        print(
            "Non-empty hypotheses -- utterances for which output was produced:",
            file=file,
        )
        for dets in top_non_empty:
            print("{key} %WER {WER:.2f}".format(**dets), file=file)
    else:
        print("No utterances which had produced output!", file=file)
    if top_empty:
        print(
            "Empty hypotheses -- utterances for which no output was produced:",
            file=file,
        )
        for dets in top_empty:
            print("{key} %WER {WER:.2f}".format(**dets), file=file)
    else:
        print("No utterances which had not produced output!", file=file)

def _print_top_wer_spks(spks_by_wer, file=sys.stdout):
    print("=" * 80, file=file)
    print("SPEAKERS WITH HIGHEST WER", file=file)
    for dets in spks_by_wer:
        print("{speaker} %WER {WER:.2f}".format(**dets), file=file)

def _print_alignment(
    alignment, a, b, empty_symbol="<eps>", separator=" ; ", file=sys.stdout
):
    # First, get equal length text for all:
    a_padded = []
    b_padded = []
    ops_padded = []
    for op, i, j in alignment:  # i indexes a, j indexes b
        op_string = str(op)
        a_string = str(a[i]) if i is not None else empty_symbol
        b_string = str(b[j]) if j is not None else empty_symbol
        # NOTE: the padding does not actually compute printed length,
        # but hopefully we can assume that printed length is
        # at most the str len
        pad_length = max(len(op_string), len(a_string), len(b_string))
        a_padded.append(a_string.center(pad_length))
        b_padded.append(b_string.center(pad_length))
        ops_padded.append(op_string.center(pad_length))
    # Then print, in the order Ref, op, Hyp
    print(separator.join(a_padded), file=file)
    print(separator.join(ops_padded), file=file)
    print(separator.join(b_padded), file=file)

def _print_alignments_global_header(
    empty_symbol="<eps>", separator=" ; ", file=sys.stdout
):
    print("=" * 80, file=file)
    print("ALIGNMENTS", file=file)
    print("", file=file)
    print("Format:", file=file)
    print("<utterance-id>, WER DETAILS", file=file)
    # Print the format with the actual
    # print_alignment function, using artificial data:
    a = ["reference", "on", "the", "first", "line"]
    b = ["and", "hypothesis", "on", "the", "third"]
    alignment = [
        (edit_distance.EDIT_SYMBOLS["ins"], None, 0),
        (edit_distance.EDIT_SYMBOLS["sub"], 0, 1),
        (edit_distance.EDIT_SYMBOLS["eq"], 1, 2),
        (edit_distance.EDIT_SYMBOLS["eq"], 2, 3),
        (edit_distance.EDIT_SYMBOLS["sub"], 3, 4),
        (edit_distance.EDIT_SYMBOLS["del"], 4, None),
    ]
    _print_alignment(
        alignment,
        a,
        b,
        file=file,
        empty_symbol=empty_symbol,
        separator=separator,
    )

def _print_alignment_header(wer_details, file=sys.stdout):
    print("=" * 80, file=file)
    print(
        "{key}, %WER {WER:.2f} [ {num_edits} / {num_ref_tokens}, {insertions} ins, {deletions} del, {substitutions} sub ]".format(  # noqa
            **wer_details
        ),
        file=file,
    )
    
    
def print_log_csv(details_by_utterance, id, file=sys.stdout):
    results = {}
    first = True
    for index, det in enumerate(details_by_utterance):
        if det['key'] == id:
            if first:
                if 'ground_truth' not in results:
                    results['ground_truth'] = ' '.join(det['ref_tokens'])
                if 'prediction' not in results:
                    results['prediction'] = []
                    results['prediction'].append(' '.join(det['hyp_tokens']))
                first = False
            else:
                if "target" not in results:
                    results['target'] = ' '.join(det['ref_tokens'])
                if 'prediction' not in results:
                    results['prediction'] = []
                results['prediction'].append(' '.join(det['hyp_tokens']))
                
    # if os.stat(file).st_size == 0:
    file.seek(0)
    is_file_empty = len(file.read()) == 0
    file.seek(0,2)
    if is_file_empty:
        header = ['id', 'iter', 'ground_truth', 'target', 
                'prediction', 'gt_wer', 'tg_wer'
            ]
    
        print(",".join(header), file=file)
    
    for it, pre in enumerate(results['prediction']):
        print("{},{},{},{},{},{},{}".format(
            id,
            it,
            results['ground_truth'],
            results['target'],
            pre,
            wer(results['ground_truth'], pre)*100,
            wer(results['target'], pre)*100
        ), file=file)
        

def print_snr_csv(metrics_detail, file=sys.stdout):
    file.seek(0)
    is_file_empty = len(file.read()) == 0
    file.seek(0,2)
    if is_file_empty:
        header = ['id', 'iter', 'snr']
    
        print(",".join(header), file=file)

    for score, (id,iter_idx) in metrics_detail:
        print("{},{},{}".format(
            id,
            iter_idx,
            score
        ), file=file)
