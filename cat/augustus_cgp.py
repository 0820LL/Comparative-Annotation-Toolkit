"""
 file:    augustus_cgp.py
 descr.:  runs AugustusCGP on input HAL file
          optionally, a database with extrinsic evidence can be provided
          for parallel computing the HAL alignment is split into overlapping alignment chunks.
          Gene prediction chunks are merged with the auxiliary tool 'joingenes' from the
          Augustus package. The output is one gff file for each species in the clade
          (or the set of target genomes)

 authors: Stefanie Koenig, Ian Fiddes
"""

import argparse
import os
import collections

from toil.fileStore import FileID
from toil.common import Toil
from toil.job import Job

import tools.misc
import tools.toilInterface
import tools.dataOps
import tools.fileOps
import tools.intervals
import tools.procOps
import tools.sqlInterface
import tools.transcripts


def augustus_cgp(args, toil_options):
    """
    Main entry function for AugustusCGP toil pipeline
    :param args: dictionary of arguments from CAT
    :param toil_options: toil options Namespace object
    :return:
    """
    with Toil(toil_options) as t:
        if not t.options.restart:
            input_file_ids = argparse.Namespace()
            input_file_ids.hal = FileID.forPath(t.importFile('file://' + args.hal), args.hal)
            input_file_ids.chrom_sizes = FileID.forPath(t.importFile('file://' + args.query_sizes), args.query_sizes)
            input_file_ids.hints_db = FileID.forPath(t.importFile('file://' + args.hints_db), args.hints_db)
            input_file_ids.cgp_param = FileID.forPath(t.importFile('file://' + args.cgp_param), args.cgp_param)
            input_file_ids.cgp_cfg = FileID.forPath(t.importFile('file://' + args.cgp_cfg), args.cgp_cfg)
            input_file_ids.fasta = {genome: FileID.forPath(t.importFile('file://' + fasta), fasta)
                                    for genome, fasta in args.fasta_files.iteritems()}
            job = Job.wrapJobFn(setup, args, input_file_ids, memory='8G', disk='2G')
            results, stdout_file_ids = t.start(job)
        else:
            results, stdout_file_ids = t.restart()
        tools.fileOps.ensure_file_dir(args.stdout_file)
        with open(args.stdout_file, 'w') as outf, tools.fileOps.TemporaryFilePath() as tmp:
            for (chrom, start, chunksize), stdout_file in stdout_file_ids.iteritems():
                outf.write('## BEGIN CHUNK chrom: {} start: {} chunksize: {}\n'.format(chrom, start, chunksize))
                t.exportFile(stdout_file, 'file://' + tmp)
                for l in open(tmp):
                    outf.write(l)
        for genome, (raw_gtf_file_id, joined_gtf_file_id, joined_gp_file_id) in results.iteritems():
            tools.fileOps.ensure_file_dir(args.augustus_cgp_raw_gtf[genome])
            t.exportFile(raw_gtf_file_id, 'file://' + args.augustus_cgp_raw_gtf[genome])
            t.exportFile(joined_gtf_file_id, 'file://' + args.augustus_cgp_gtf[genome])
            t.exportFile(joined_gp_file_id, 'file://' + args.augustus_cgp_gp[genome])


def setup(job, args, input_file_ids):
    """
    Entry function for running AugustusCGP.
    HAL alignment is converted to MAF format and splitted into overlapping
    alignment chunks for parallel computing. Each alignment chunk is one child process.
    Gene predictions on alignment chunks are subsequently merged into one gff for each species.
    For merging of the gene sets, the auxiliary tool 'joingenes' from the Augustus tool package is used.
    """
    # create a file with the phylogenetic tree in NEWICK format
    tree = writeTree(job, input_file_ids)

    # results holds Promise objects
    # each Promise object will resolve to a tuple of gff_chunk_dict, stdout_file_id
    # cgp_job.rv():  key: genome, value: file handle to gff
    results = []

    # TODO: do not split within genic regions of the reference genome
    chrom_sizes = job.fileStore.readGlobalFile(input_file_ids.chrom_sizes)
    hal2maf_usage = tools.toilInterface.find_total_disk_usage(input_file_ids.hal)
    # 4G buffer for MAF chunk, should be more than enough (famous last words)
    cgp_usage = tools.toilInterface.find_total_disk_usage([input_file_ids.fasta, input_file_ids.hints_db], buffer='4G')

    for chrom, chrom_size in tools.fileOps.iter_lines(chrom_sizes):
        chrom_size = int(chrom_size)
        for start in xrange(0, chrom_size, args.chunksize - args.overlap):
            chunksize = args.chunksize if start + args.chunksize <= chrom_size else chrom_size - start
            j = job.addChildJobFn(hal2maf, input_file_ids, args.ref_genome, chrom, start, chunksize, memory='8G',
                                  disk=hal2maf_usage)
            maf_chunk = j.rv()
            # run AugustusCGP on alignment chunk
            cgp_job = j.addFollowOnJobFn(cgp, tree, maf_chunk, args, input_file_ids, memory='8G', disk=cgp_usage)
            results.append([chrom, start, chunksize, cgp_job.rv()])

    # merge all gff files for alignment chunks to one gff for each species
    # results is a 2-member tuple of a joined genes list and a stdout file id dict
    # stdout_file_id dict is keyed by (chromosome, start, chunksize) tuples
    # for the joined genes dict its a dict keyed by genome and values are a 3 member tuple of:
    # [raw_gtf_file_id, joined_gtf_file_id, joined_gp_file_id]
    results = job.addFollowOnJobFn(merge_results, results, memory='8G', disk='8G').rv()
    return results


def hal2maf(job, input_file_ids, ref_genome, chrom, start, chunk_size):
    """
    exports hal to maf on a genomic region specified by (genome, seq, start, len)
    """
    hal = job.fileStore.readGlobalFile(input_file_ids.hal)
    maf_chunk = tools.fileOps.get_tmp_toil_file()
    cmd = ['hal2maf', '--noAncestors', '--noDupes', '--refGenome', ref_genome,
           '--refSequence', chrom, '--start', start, '--length', chunk_size, hal, maf_chunk]
    tools.procOps.run_proc(cmd)
    return job.fileStore.writeGlobalFile(maf_chunk)


def cgp(job, tree, mafChunk, args, input_file_ids):
    """
    core function that runs AugustusCGP on one alignment chunk
    """
    genomeFofn = writeGenomeFofn(job, input_file_ids.fasta)
    cgp_cfg = job.fileStore.readGlobalFile(input_file_ids.cgp_cfg)
    stdout = tools.fileOps.get_tmp_toil_file()

    cmd = ['augustus', '--dbhints=1', '--UTR=1', '--allow_hinted_splicesites=atac',
           '--extrinsicCfgFile={}'.format(cgp_cfg),
           '--species={}'.format(args.species),
           '--treefile={}'.format(job.fileStore.readGlobalFile(tree)),
           '--alnfile={}'.format(job.fileStore.readGlobalFile(mafChunk)),
           '--dbaccess={}'.format(job.fileStore.readGlobalFile(input_file_ids.hints_db)),
           '--speciesfilenames={}'.format(genomeFofn),
           '--softmasking=1',
           '--exoncands=0',
           '--alternatives-from-evidence=0',
           '--/CompPred/logreg=on',
           '--printOEs=false',
           '--/CompPred/outdir={}'.format(os.getcwd()),
           '--optCfgFile={}'.format(job.fileStore.readGlobalFile(input_file_ids.cgp_param))]
    tools.procOps.run_proc(cmd, stdout=stdout)
    stdout_file_id = job.fileStore.writeGlobalFile(stdout)
    return {genome: job.fileStore.writeGlobalFile(genome + '.cgp.gff') for genome in args.genomes}, stdout_file_id


def merge_results(job, results):
    """
    Results is a list of lists in the form [chrom, start, chunksize, (gff_chunk_dict, stdout_file_id)]
    gff_chunk is a dict of {genome: gff_file_id}
    Merges the results using joinGenes.
    """
    # reshape results into a dict of dicts:
    # {genome: (chrom, start, chunksize): gff_file_id
    gff_chunks_by_genome = collections.defaultdict(dict)
    stdout_file_ids = {}
    for chrom, start, chunksize, (gff_chunks, stdout_file_id) in results:
        stdout_file_ids[(chrom, start, chunksize)] = stdout_file_id
        for genome, gff_file_id in gff_chunks.iteritems():
            gff_chunks_by_genome[genome][(chrom, start, chunksize)] = gff_file_id
    results = {}
    for genome in gff_chunks_by_genome:
        j = job.addChildJobFn(join_genes, gff_chunks_by_genome[genome], memory='8G', disk='8G')
        results[genome] = j.rv()
    return results, stdout_file_ids


def join_genes(job, gff_chunks):
    """
    uses the auxiliary tool 'joingenes' from the
    Augustus package to intelligently merge gene sets
    - removes duplicated Txs or truncated Txs that are contained in other Txs (trivial)
    - fixes truncated Txs at alignment boundaries,
      e.g. by merging them with other Txs (non trivial, introduces new Txs)
    """
    raw_gtf_file = tools.fileOps.get_tmp_toil_file()
    raw_gtf_fofn = tools.fileOps.get_tmp_toil_file()
    with open(raw_gtf_file, 'w') as raw_handle, open(raw_gtf_fofn, 'w') as fofn_handle:
        for (chrom, start, chunksize), chunk in gff_chunks.iteritems():
            local_path = job.fileStore.readGlobalFile(chunk)
            fofn_handle.write(local_path + '\n')
            raw_handle.write('## BEGIN CHUNK chrom: {} start: {} chunksize: {}\n'.format(chrom, start, chunksize))
            for line in open(local_path):
                raw_handle.write(line)

    join_genes_file = tools.fileOps.get_tmp_toil_file()
    join_genes_gp = tools.fileOps.get_tmp_toil_file()
    cmd = [['joingenes', '-f', raw_gtf_fofn, '-o', '/dev/stdout'],
           ['grep', '-P', '\tAUGUSTUS\t(exon|CDS|start_codon|stop_codon|tts|tss)\t'],
           ['sed', ' s/jg/augCGP-/g']]
    tools.procOps.run_proc(cmd, stdout=join_genes_file)

    # passing the joingenes output through gtfToGenePred then genePredToGtf fixes the sort order for homGeneMapping
    cmd = ['gtfToGenePred', '-genePredExt', join_genes_file, join_genes_gp]
    tools.procOps.run_proc(cmd)
    cmd = ['genePredToGtf', 'file', join_genes_gp, '-utr', '-honorCdsStat', '-source=augustusCGP', join_genes_file]
    tools.procOps.run_proc(cmd)

    joined_gtf_file_id = job.fileStore.writeGlobalFile(join_genes_file)
    raw_gtf_file_id = job.fileStore.writeGlobalFile(raw_gtf_file)
    joined_gp_file_id = job.fileStore.writeGlobalFile(join_genes_gp)
    return raw_gtf_file_id, joined_gtf_file_id, joined_gp_file_id


###
# Accessory functions
###


def writeTree(job,input_file_ids):
    """
    writes a file with the phylogenetic tree in NEWICK format
    """
    hal = job.fileStore.readGlobalFile(input_file_ids.hal) 
    cmd = ['halStats', '--tree', hal]
    tree = tools.fileOps.get_tmp_toil_file()
    tools.procOps.run_proc(cmd, stdout=tree)
    return job.fileStore.writeGlobalFile(tree)


def writeGenomeFofn(job, fasta_file_ids):
    """
    writes a file with the location of the fasta files, e.g.

    galGal4 /path/to/genome/galGal4.fa
    hg38    /path/to/genome/hg38.fa
    mm10    /path/to/genome/mm10.fa
    rn6     /path/to/genome/rn6.fa
    ...

    These files are loaded from the fileStore
    """
    genomeFofn = tools.fileOps.get_tmp_toil_file()
    with open(genomeFofn, 'w') as outf:
        for genome, file_id in fasta_file_ids.iteritems():
            local_path = job.fileStore.readGlobalFile(file_id)
            tools.fileOps.print_row(outf, [genome, local_path])
    return genomeFofn


