"""
Copyright 2019 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Trycycler

This file is part of Trycycler. Trycycler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Trycycler is distributed
in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Trycycler.
If not, see <http://www.gnu.org/licenses/>.
"""

import collections
import sys

from .alignment import align_reads_to_seq
from .log import log, section_header, explanation
from .misc import get_sequence_file_type, load_fasta, get_fastq_stats, range_overlap
from .software import check_minimap2
from . import settings


def consensus(args):
    welcome_message()
    check_inputs_and_requirements(args)

    seqs, seq_names, seq_lengths = load_seqs(args.cluster_dir)
    msa_seqs, msa_names, msa_length = load_msa(args.cluster_dir)
    sanity_check_msa(seqs, seq_names, seq_lengths, msa_seqs, msa_names, msa_length)

    chunks = partition_msa(msa_seqs, msa_names, msa_length, settings.CHUNK_COMBINE_SIZE)
    save_chunks_to_gfa(chunks, args.cluster_dir / '5_chunked_sequence.gfa', len(msa_names))

    consensus_seq_with_gaps, consensus_seq_without_gaps = make_initial_consensus(chunks)
    save_seqs_to_fasta({args.cluster_dir.name + '_consensus': consensus_seq_without_gaps},
                       args.cluster_dir / '6_initial_consensus.fasta')

    circular = not args.linear
    index_reads(args.cluster_dir, chunks, consensus_seq_with_gaps, consensus_seq_without_gaps,
                circular, args.threads, args.min_read_cov, args.min_aligned_len)
    choose_best_chunk_options(chunks)


def welcome_message():
    section_header('Starting Trycycler consensus')
    explanation('Trycycler consensus is a tool for combining multiple contigs from the same '
                'long-read set (e.g. assemblies from different assemblers) into a consensus '
                'contig that takes the best parts of each.')


def check_inputs_and_requirements(args):
    check_input_reads(args.cluster_dir)
    check_cluster_directory(args.cluster_dir)
    check_seqs(args.cluster_dir)
    check_required_software()


def partition_msa(msa_seqs, seq_names, msa_length, combine_size):
    section_header('Partitioning MSA')
    explanation('The multiple sequence alignment is now partitioned into chunks, where the '
                'sequence is all in agreement ("same" chunks) or not ("different" chunks).')

    chunks, chunk_count = [], 0
    current_chunk = Chunk()

    for i in range(msa_length):
        bases = {n: msa_seqs[n][i] for n in seq_names}
        if not current_chunk.can_add_bases(bases):
            chunks.append(current_chunk)
            chunk_count += 1
            if chunk_count % 100 == 0:
                log(f'\rchunks: {chunk_count:,}', end='')
            current_chunk = Chunk()
        current_chunk.add_bases(bases)

    if not current_chunk.is_empty():
        chunks.append(current_chunk)
        chunk_count += 1

    same_count = len([c for c in chunks if c.type == 'same'])
    different_count = len([c for c in chunks if c.type == 'different'])
    log(f'\rchunks: {chunk_count:,} ({same_count:,} same, {different_count:,} different)', end='')
    log()

    sanity_check_chunks(chunks, msa_length)
    chunks = combine_chunks(chunks, combine_size)
    sanity_check_chunks(chunks, msa_length)
    log()

    return chunks


def make_initial_consensus(chunks):
    log('Producing an initial consensus using the most common sequence for each chunk:')
    total_length = 0
    for i, chunk in enumerate(chunks):
        chunk.set_best_seq_as_most_common()
        assert chunk.best_seq is not None
        total_length += len(chunk.best_seq.replace('-', ''))
        log(f'\r  consensus length: {total_length:,} bp', end='')
    log('\n')
    consensus_seq_with_gaps = ''.join([c.best_seq for c in chunks])
    consensus_seq_without_gaps = consensus_seq_with_gaps.replace('-', '')
    return consensus_seq_with_gaps, consensus_seq_without_gaps


def index_reads(cluster_dir, chunks, consensus_seq_with_gaps, consensus_seq_without_gaps,
                circular, threads, min_read_cov, min_aligned_len):
    section_header('Indexing reads')
    explanation('Trycycler now aligns all reads to the initial consensus to form an index of '
                'which reads will be informative to each of the chunks.')

    ungapped_to_gapped = make_ungapped_pos_to_gapped_pos_dict(consensus_seq_with_gaps,
                                                              consensus_seq_without_gaps)
    reads = cluster_dir / '4_reads.fastq'
    ungapped_len = len(consensus_seq_without_gaps)
    gapped_len = len(consensus_seq_with_gaps)

    log('Aligning reads to initial consensus:')
    if circular:
        ref_seq = consensus_seq_without_gaps + consensus_seq_without_gaps
        alignments = align_reads_to_seq(reads, ref_seq, threads)
        alignments = [a for a in alignments if a.ref_start < ungapped_len]
    else:
        ref_seq = consensus_seq_without_gaps
        alignments = align_reads_to_seq(reads, ref_seq, threads)
    log(f'  {len(alignments):,} alignments')
    log()

    log('Filtering for best alignment per read:')
    alignments = get_best_alignment_per_read(alignments)
    alignments = [a for a in alignments if a.query_cov >= min_read_cov
                  and a.query_end - a.query_start >= min_aligned_len]
    log(f'  {len(alignments):,} alignments')
    log()

    different_chunk_count = len([c for c in chunks if c.type == 'different'])
    chunk_start = 0
    completed = 0
    log(f'\rGathering reads for chunks: {completed:,} / {different_chunk_count:,}', end='')
    for chunk in chunks:
        chunk_end = chunk_start + chunk.get_length()
        if chunk.type == 'different':
            for a in alignments:
                gapped_start = ungapped_to_gapped[a.ref_start]
                if a.ref_end <= ungapped_len:  # if we're not spanning the circular gap
                    gapped_end = ungapped_to_gapped[a.ref_end]
                    if range_overlap(chunk_start, chunk_end, gapped_start, gapped_end):
                        chunk.read_names.add(a.query_name)
                else:  # if we are spanning the circular gap
                    gapped_end = ungapped_to_gapped[a.ref_end - ungapped_len]
                    if range_overlap(chunk_start, chunk_end, gapped_start, gapped_len) or \
                            range_overlap(chunk_start, chunk_end, 0, gapped_end):
                        chunk.read_names.add(a.query_name)
            completed += 1
            log(f'\rGathering reads for chunks: {completed:,} / {different_chunk_count:,}', end='')
        chunk_start = chunk_end
    assert chunk_start == len(consensus_seq_with_gaps)
    log('\n')


def make_ungapped_pos_to_gapped_pos_dict(consensus_seq_with_gaps, consensus_seq_without_gaps):
    ungapped_to_gapped = {}
    ungapped_pos = 0
    for i, base in enumerate(consensus_seq_with_gaps):
        ungapped_to_gapped[ungapped_pos] = i
        if base != '-':
            ungapped_pos += 1
    assert ungapped_pos == len(consensus_seq_without_gaps)
    return ungapped_to_gapped


def get_best_alignment_per_read(alignments):
    alignments_per_read = collections.defaultdict(list)
    for a in alignments:
        alignments_per_read[a.query_name].append(a)
    best_alignments = []
    for read_name, alignments in alignments_per_read.items():
        alignments = sorted(alignments, key=lambda x: x.alignment_score, reverse=True)
        best_alignments.append(alignments[0])
    return best_alignments


def choose_best_chunk_options(chunks):
    for i, chunk in enumerate(chunks):
        pass
    # TODO
    # TODO
    # TODO
    # TODO
    # TODO
    # TODO
    # TODO
    # TODO
    # TODO
    # TODO


def sanity_check_chunks(chunks, msa_length):
    """
    Makes sure that chunks alternate in type: same, different, same, different, etc.
    """
    total_length = 0
    for i, chunk in enumerate(chunks):
        assert chunk.type is not None
        if i > 0:
            prev_chunk = chunks[i-1]
            if chunk.type == 'same':
                assert prev_chunk.type == 'different'
            elif chunk.type == 'different':
                assert prev_chunk.type == 'same'
            else:
                assert False
        total_length += chunk.get_length()
    assert total_length == msa_length


def combine_chunks(chunks, combine_size):
    """
    This function combines chunks when there is a very small 'same' chunk between two 'different'
    chunks.
    """
    log('combining small chunks: ', end='')
    combined_chunks = []
    for i, chunk in enumerate(chunks):
        if i == 0 or chunk.type == 'different':
            combined_chunks.append(chunk)
        else:
            assert chunk.type == 'same'
            assert combined_chunks[-1].type == 'different'
            if chunk.get_length() <= combine_size:
                combined_chunks[-1].add_one_seq_to_seqs(chunk.seq)
            else:
                combined_chunks.append(chunk)

    # We are now in a position where two adjacent chunks might both be 'different' chunks, so
    # we need to merge them together.
    new_chunks = []
    for i, chunk in enumerate(combined_chunks):
        if i == 0 or chunk.type == 'same':
            new_chunks.append(chunk)
        else:
            assert chunk.type == 'different'
            if new_chunks[-1].type == 'different':
                new_chunks[-1].add_multiple_seqs_to_seqs(chunk.seqs)
            else:
                new_chunks.append(chunk)

    same_count = len([c for c in new_chunks if c.type == 'same'])
    different_count = len([c for c in new_chunks if c.type == 'different'])
    log(f'{len(new_chunks):,} ({same_count:,} same, {different_count:,} different)')
    return new_chunks


class Chunk(object):
    """
    This class holds a chunk of the MSA, which can either be a 'same' chunk (where all sequences
    agree) or a 'different' chunk (where there is at least one difference).
    """
    def __init__(self):
        self.type = None  # will be either 'same' or 'different'
        self.seq = None  # will hold the sequence for a 'same' chunk
        self.seqs = None  # will hold the multiple alternative sequences for a 'different' chunk
        self.read_names = set()  # will hold read names relevant for assessing this chunk
        self.best_seq = None

    def add_bases(self, bases):
        assert self.can_add_bases(bases)

        # If this is a new chunk, we'll set its type now.
        if self.type is None:
            base_count = len(set(bases.values()))
            if base_count == 1:
                self.type = 'same'
                self.seq = []
            else:
                self.type = 'different'
                self.seqs = {n: [] for n in bases.keys()}

        if self.type == 'same':
            base = list(bases.values())[0]
            self.seq.append(base)

        else:
            assert self.type == 'different'
            for name, base in bases.items():
                self.seqs[name].append(base)

    def can_add_bases(self, bases):
        """
        Tests to see whether the given bases are incompatible with the current chunk type.
        """
        if self.type is None:
            return True
        base_count = len(set(bases.values()))
        if self.type == 'same' and base_count == 1:
            return True
        if self.type == 'different' and base_count > 1:
            return True
        return False

    def is_empty(self):
        return self.type is None

    def get_length(self):
        if self.type is None:
            return 0
        elif self.type == 'same':
            return len(self.seq)
        elif self.type == 'different':
            lengths = set(len(seq) for seq in self.seqs.values())
            assert len(lengths) == 1  # all seqs should be the same length
            return list(lengths)[0]
        else:
            assert False

    def __str__(self):
        if self.type is None:
            return ''
        elif self.type == 'same':
            return ''.join(self.seq)
        elif self.type == 'different':
            seq_lines = []
            longest_name = max(len(name) for name in self.seqs.keys())
            for name, seq in self.seqs.items():
                seq = ''.join(seq)
                seq_lines.append(f'{name.rjust(longest_name)}: {seq}')
            return '\n'.join(seq_lines)

    def add_one_seq_to_seqs(self, additional_seq):
        assert self.type == 'different'
        new_seqs = {}
        for name, seq in self.seqs.items():
            new_seqs[name] = seq + additional_seq
        self.seqs = new_seqs

    def add_multiple_seqs_to_seqs(self, additional_seqs):
        assert self.type == 'different'
        new_seqs = {}
        for name, seq in self.seqs.items():
            new_seqs[name] = seq + additional_seqs[name]
        self.seqs = new_seqs

    def set_best_seq_as_most_common(self):
        self.best_seq = self.get_most_common_seq()

    def get_most_common_seq(self):
        """
        Returns the chunk's 'best' sequence as a string. For 'same' chunks, this simply means the
        chunk sequence. For 'different' chunks, this is the most common sequence. If there is a tie
        for the most common sequence, then we choose whichever sequence has the lowest total
        distance to the other sequences.
        """
        if self.type is None:
            return ''
        elif self.type == 'same':
            return ''.join(self.seq)
        else:
            assert self.type == 'different'
            options = [''.join(seq) for seq in self.seqs.values()]
            option_counts = list(collections.Counter(options).items())
            assert len(option_counts) > 1  # a 'different' chunk has multiple options by definition
            option_counts = sorted(option_counts, key=lambda x: x[1], reverse=True)
            best_count = option_counts[0][1]
            best_options = [x[0] for x in option_counts if x[1] == best_count]

            # If there is a clear winner, then we return that.
            if len(best_options) == 1:
                return best_options[0]

            # If there are multiple sequences which tie for the best, then we choose the one with
            # the smallest total Hamming distance to the other options.
            hamming_distances = {x: 0 for x in best_options}
            for x in best_options:
                for y in options:
                    hamming_distances[x] += hamming_distance(x, y)
            hamming_distances = sorted(hamming_distances.items(), key=lambda x: x[1])
            best_distance = hamming_distances[0][1]
            best_options = [x[0] for x in hamming_distances if x[1] == best_distance]
            if len(best_options) == 1:
                return best_options[0]

            # If there are still multiple sequences, we return the lexicographically first one.
            return sorted(best_options)[0]


def hamming_distance(s1, s2):
    dist = 0
    for i in range(len(s1)):
        if s1[i] != s2[i]:
            dist += 1
    return dist


























def check_input_reads(cluster_dir):
    filename = cluster_dir / '4_reads.fastq'
    read_type = get_sequence_file_type(filename)
    if read_type != 'FASTQ':
        sys.exit(f'\nError: input reads ({filename}) are not in FASTQ format')
    log(f'Input reads: {filename}')
    read_count, total_size, n50 = get_fastq_stats(filename)
    log(f'  {read_count:,} reads ({total_size:,} bp)')
    log(f'  N50 = {n50:,} bp')
    log()


def check_seqs(cluster_dir):
    filename = cluster_dir / '2_all_seqs.fasta'
    log(f'Input contigs: {filename}')
    contig_type = get_sequence_file_type(filename)
    if contig_type != 'FASTA':
        sys.exit(f'\nError: input contig file ({filename}) is not in FASTA format')
    seqs = load_fasta(filename)
    if len(seqs) == 0:
        sys.exit(f'\nError: input contig file ({filename}) contains no sequences')
    contig_names = set()
    for contig_name, seq in seqs:
        if contig_name in contig_names:
            sys.exit(f'\nError: duplicate contig name: {contig_name}')
        contig_names.add(contig_name)
        log(f'  {contig_name}: {len(seq):,} bp')
    log()


def check_cluster_directory(directory):
    if directory.is_file():
        sys.exit(f'\nError: output directory ({directory}) already exists as a file')
    if not directory.is_dir():
        sys.exit(f'\nError: output directory ({directory}) does not exist')

    seq_file = directory / '2_all_seqs.fasta'
    if not seq_file.is_file():
        sys.exit(f'\nError: output directory ({directory}) does not contain2_all_seqs.fasta')

    pairwise_file = directory / '3_msa.fasta'
    if not pairwise_file.is_file():
        sys.exit(f'\nError: output directory ({directory}) does not contain 3_msa.fasta')

    reads_file = directory / '4_reads.fastq'
    if not reads_file.is_file():
        sys.exit(f'\nError: output directory ({directory}) does not contain 4_reads.fastq')


def check_required_software():
    log('Checking required software:')
    check_minimap2()
    log()


def load_seqs(cluster_dir):
    filename = cluster_dir / '2_all_seqs.fasta'
    seqs = dict(load_fasta(filename))
    seq_names = sorted(seqs.keys())
    seq_lengths = {name: len(seq) for name, seq in seqs.items()}
    return seqs, seq_names, seq_lengths


def load_msa(cluster_dir):
    filename = cluster_dir / '3_msa.fasta'
    seqs = dict(load_fasta(filename))
    seq_names = sorted(seqs.keys())
    seq_lengths = {name: len(seq) for name, seq in seqs.items()}

    seq_length_set = set(seq_lengths.values())
    assert len(seq_length_set) == 1
    msa_length = list(seq_length_set)[0]

    return seqs, seq_names, msa_length


def sanity_check_msa(seqs, seq_names, seq_lengths, msa_seqs, msa_names, msa_length):
    assert seq_names == msa_names
    for n in seq_names:
        assert seq_lengths[n] <= msa_length
        assert seqs[n] == msa_seqs[n].replace('-', '')


def save_seqs_to_fasta(seqs, filename, extra_newline=True):
    seq_word = 'sequence' if len(seqs) == 1 else 'sequences'
    log(f'Saving {seq_word} to file: {filename}')
    with open(filename, 'wt') as fasta:
        for name, seq in seqs.items():
            fasta.write(f'>{name}\n')
            fasta.write(f'{seq}\n')
    if extra_newline:
        log()


def save_chunks_to_gfa(chunks, filename, input_count, extra_newline=True):
    chunk_word = 'sequence' if len(chunks) == 1 else 'sequences'
    log(f'Saving {chunk_word} to graph: {filename}')
    with open(filename, 'wt') as gfa:
        gfa.write('H\tVN:Z:1.0\tbn:Z:--linear --singlearr\n')  # header line with Bandage options
        link_lines = []
        prev_chunk_names = None
        for i, chunk in enumerate(chunks):
            if chunk.type == 'same':
                assert chunk.seq is not None
                chunk_seq = ''.join(chunk.seq)
                chunk_name = str(i+1)
                gfa.write(f'S\t{chunk_name}\t{chunk_seq}\tdp:f:{input_count}\n')
                if prev_chunk_names is not None:
                    assert len(prev_chunk_names) > 1  # same chunks are preceded by diff chunks
                    for prev_name in prev_chunk_names:
                        link_lines.append(f'L\t{prev_name}\t+\t{chunk_name}\t+\t0M\n')
                prev_chunk_names = [chunk_name]

            elif chunk.type == 'different':
                assert chunk.seqs is not None
                chunk_seq_counts = collections.defaultdict(int)
                chunk_names = []
                for s in chunk.seqs.values():
                    chunk_seq_counts[''.join(s)] += 1
                j = 1
                for chunk_seq, count in chunk_seq_counts.items():
                    chunk_name = f'{i+1}_{j}'
                    chunk_names.append(chunk_name)
                    gfa.write(f'S\t{chunk_name}\t{chunk_seq}\tdp:f:{count}\n')
                    j += 1
                if prev_chunk_names is not None:
                    assert len(prev_chunk_names) == 1  # diff chunks are preceded by same chunks
                    prev_name = prev_chunk_names[0]
                    for chunk_name in chunk_names:
                        link_lines.append(f'L\t{prev_name}\t+\t{chunk_name}\t+\t0M\n')
                prev_chunk_names = chunk_names
            else:
                assert False
        for link_line in link_lines:
            gfa.write(link_line)

    if extra_newline:
        log()
