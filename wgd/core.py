import uuid
import os
import logging
import numpy as np
import pandas as pd
import subprocess as sp
import fastcluster
from Bio import SeqIO
from Bio import AlignIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import generic_dna
from Bio.Align import MultipleSeqAlignment
from Bio.Data.CodonTable import TranslationError
from Bio import Phylo
from joblib import Parallel, delayed
from wgd.codeml import Codeml


# helper functions
def _write_fasta(fname, seq_dict):
    with open(fname, "w") as f:
        for k, v in seq_dict.items():
            f.write(">{}\n{}\n".format(k, v.seq))
    return fname

def _mkdir(dirname):
    if os.path.isdir(dirname):
        logging.warning("dir {} exists!".format(dirname))
    else:
        os.mkdir(dirname)
    return dirname

def _strip_gaps(aln):
    new_aln = aln[:,0:0]
    for j in range(aln.get_alignment_length()):
        if any([x == "-" for x in aln[:,j]]):
            continue
        else:
            new_aln += aln[:,j:j+1]
    return new_aln

def _pal2nal(pro_aln, cds_seqs):
    aln = {}
    for i, s in enumerate(pro_aln):
        cds_aln = ""
        cds_seq = cds_seqs[s.id].seq
        k = 0
        for j in range(pro_aln.get_alignment_length()):
            if pro_aln[i, j] == "-":
                cds_aln += "---"
            elif pro_aln[i, j] == "X":
                cds_aln += "???"  # not sure wha best choice for codeml is
                k += 3
            else:
                cds_aln += cds_seq[k:k+3]
                k += 3
        aln[s.id] = cds_aln
    return MultipleSeqAlignment([SeqRecord(v, id=k) for k, v in aln.items()])

def _write_aln_codeml(aln, fname):
    with open(fname, "w") as f:
        f.write("{} {}\n".format(len(aln), aln.get_alignment_length()))
        for s in aln:
            f.write("{}\n".format(s.id))
            f.write("{}\n".format(s.seq))

def _log_process(o, program=""):
    logging.debug("{} stderr: {}".format(program.upper(), o.stderr.decode()))
    logging.debug("{} stdout: {}".format(program.upper(), o.stdout.decode()))

def _cluster(df, nanpolicy=1000):
    # fill NaN values with something larger than all the rest, not a
    # foolproof approach, but should be reasonable in most cases
    if np.any(np.isnan(df)):
        logging.warning("Data contains NaN values, replaced by "+str(nanpolicy))
        df.fillna(nanpolicy, inplace=True)
    return fastcluster.average(df)

def _label_internals(tree):
    for i, c in enumerate(tree.get_nonterminals()):
        c.name = str(i)


# keep in dict with keys safe ids, with as values the full record, allowing at
# all time full recovery of gene names etc.?
class SequenceData:
    """
    Sequence data container for Ks distribution computation pipeline. A helper
    class that bundles sequence manipulation methods.
    """
    def __init__(self, cds_fasta, tmp_path=None, out_path="wgd_dmd",
            to_stop=True, cds=True):
        if tmp_path == None:
            tmp_path = str(uuid.uuid4())
        self.tmp_path  = _mkdir(tmp_path)
        self.out_path  = _mkdir(out_path)
        self.cds_fasta = cds_fasta
        self.prefix = os.path.basename(self.cds_fasta)
        self.pro_fasta = os.path.join(tmp_path, self.prefix + ".tfa")
        self.pro_db = os.path.join(tmp_path, self.prefix + ".db")
        self.cds_seqs = {}
        self.pro_seqs = {}
        self.dmd_hits = {}
        self.rbh = {}
        self.mcl = {}
        self.read_cds(to_stop=to_stop, cds=cds)
        _write_fasta(self.pro_fasta, self.pro_seqs)

    def read_cds(self, to_stop=True, cds=True):
        for i, seq in enumerate(SeqIO.parse(self.cds_fasta, 'fasta')):
            gid = "{0}_{1:0>5}".format(self.prefix, i)
            try:
                aa_seq = seq.translate(to_stop=to_stop, cds=cds, id=seq.id)
            except TranslationError as e:
                logging.error("Translation error ({}) in seq {}".format(
                    e, seq.id))
                continue
            self.cds_seqs[gid] = seq
            self.pro_seqs[gid] = aa_seq
        return

    def make_diamond_db(self):
        cmd = ["diamond", "makedb", "--in", self.pro_fasta, "-d", self.pro_db]
        out = sp.run(cmd, capture_output=True)
        logging.debug(out.stderr.decode())
        if out.returncode == 1:
            logging.error(out.stderr.decode())

    def run_diamond(self, seqs, eval=1e-10):
        self.make_diamond_db()
        run = "_".join([self.prefix, seqs.prefix + ".tsv"])
        outfile = os.path.join(self.tmp_path, run)
        cmd = ["diamond", "blastp", "-d", self.pro_db, "-q",
            seqs.pro_fasta, "-o", outfile]
        out = sp.run(cmd, capture_output=True)
        logging.debug(out.stderr.decode())
        df = pd.read_csv(outfile, sep="\t", header=None)
        df = df.loc[df[0] != df[1]]
        self.dmd_hits[seqs.prefix] = df = df.loc[df[10] <= eval]
        return df

    def get_rbh_orthologs(self, seqs, eval=1e-10):
        if self == seqs:
            raise ValueError("RBH orthologs only defined for distinct species")
        df = self.run_diamond(seqs, eval=eval)
        df1 = df.sort_values(10).drop_duplicates([0])
        df2 = df.sort_values(10).drop_duplicates([1])
        self.rbh[seqs.prefix] = df1.merge(df2)
        # self.rbh[seqs.prefix] = seqs.rbh[self.prefix] = df1.merge(df2)
        # write to file using original ids for next steps

    def get_paranome(self, inflation=1.5, eval=1e-10):
        df = self.run_diamond(self, eval=eval)
        gf = self.get_mcl_graph(self.prefix)
        mcl_out = gf.run_mcl(inflation=inflation)
        with open(mcl_out, "r") as f:
            for i, line in enumerate(f.readlines()):
                self.mcl[i] = line.strip().split()

    def get_mcl_graph(self, *args):
        # args are keys in `self.dmd_hits` to use for building MCL graph
        gf = os.path.join(self.tmp_path, "_".join([self.prefix] + list(args)))
        df = pd.concat([self.dmd_hits[x] for x in args])
        df.to_csv(gf, sep="\t", header=False, index=False, columns=[0,1,10])
        return SequenceSimilarityGraph(gf)

    def write_paranome(self):
        fname = os.path.join(self.out_path, "{}.mcl".format(self.prefix))
        with open(fname, "w") as f:
            for k, v in sorted(self.mcl.items()):
                f.write("\t".join([self.cds_seqs[x].id for x in v]))
                f.write("\n")
        return fname

    def write_rbh_orthologs(self, seqs):
        prefix = seqs.prefix
        fname = "{}_{}.rbh".format(self.prefix, prefix)
        fname = os.path.join(self.out_path, fname)
        df = self.rbh[prefix]
        df["x"] = df[0].apply(lambda x: seqs.cds_seqs[x].id)
        df["y"] = df[1].apply(lambda x: self.cds_seqs[x].id)
        df.to_csv(fname, columns=["x", "y"], header=None, index=False, sep="\t")
        # header=[prefix, self.prefix]

    def remove_tmp(self, prompt=True):
        if prompt:
            ok = input("Removing {}, sure? [y|n]".format(self.tmp_path))
            if ok != "y":
                return
        out = sp.run(["rm", "-r", self.tmp_path], capture_output=True)
        logging.debug(out.stderr.decode())


class SequenceSimilarityGraph:
    def __init__(self, graph_file):
        self.graph_file = graph_file

    def run_mcl(self, inflation=1.5):
        g1 = self.graph_file
        g2 = g1 + ".tab"
        g3 = g1 + ".mci"
        g4 = g2 + ".I{}".format(inflation*10)
        outfile = g1 + ".mcl"
        command = ['mcxload', '-abc', g1, '--stream-mirror',
            '--stream-neg-log10', '-o', g3, '-write-tab', g2]
        logging.debug(" ".join(command))
        out = sp.run(command, capture_output=True)
        _log_process(out)
        command = ['mcl', g3, '-I', str(inflation), '-o', g4]
        logging.debug(" ".join(command))
        out = sp.run(command, capture_output=True)
        _log_process(out)
        command = ['mcxdump', '-icl', g4, '-tabr', g2, '-o', outfile]
        _log_process(out)
        out = sp.run(command, capture_output=True)
        _log_process(out)
        return outfile


def get_gene_families_paranome(seq_data, families, **kwargs):
    gene_families = {}
    for k, v in families.items():
        cds = {x: seq_data.cds_seqs[x] for x in v}
        pro = {x: seq_data.pro_seqs[x] for x in v}
        fid = "GF{:0>5}".format(k)
        tmp = os.path.join(seq_data.tmp_path, fid)
        gene_families[k] = GeneFamily(fid, cds, pro, tmp, **kwargs)
    return gene_families


def get_gene_families_rbh_orthologs(seq_data, families):
    pass


# NOTE: It would be nice to implement an option to do a full 'proper' approach
# where we use the tree in codeml to estimate Ks?
class GeneFamily:
    def __init__(self, gfid, cds, pro, tmp_path,
            aligner="mafft", tree_method="iqtree", ks_method="GY94",
            eq_freq="F3X4", kappa=None, prequal=True, strip_gaps=True,
            min_length=100, codeml_iter=1, substitution_model_iqtree=None,
            aln_options="--auto", tree_options="-m LG"):
        self.id = gfid
        self.cds_seqs = cds
        self.pro_seqs = pro
        self.tmp_path = _mkdir(tmp_path)
        self.cds_fasta = os.path.join(self.tmp_path, "cds.fasta")
        self.pro_fasta = os.path.join(self.tmp_path, "pro.fasta")
        self.cds_alnf = os.path.join(self.tmp_path, "cds.aln")
        self.pro_alnf = os.path.join(self.tmp_path, "pro.aln")
        self.cds_aln = None
        self.pro_aln = None
        self.codeml = None
        self.pairwise_estimates = None
        self.tree = None
        self.ks_out = os.path.join(self.tmp_path, "{}.csv".format(gfid))

        # config
        self.aligner = aligner  # mafft | prank | muscle
        self.tree_method = tree_method  # iqtree | fasttree | alc
        self.ks_method = ks_method  # GY | NG
        self.kappa = kappa
        self.eq_freq = eq_freq
        self.prequal = prequal
        self.strip_gaps = strip_gaps  # strip gaps based on overall alignment
        self.codeml_iter = codeml_iter
        self.min_length = min_length  # minimum length of codon alignment
        self.substitution_model_iqtree = substitution_model_iqtree
        self.aln_options = aln_options
        self.tree_options = tree_options

    def get_ks(self):
        self.align()
        self.run_codeml()
        self.get_tree()
        self.compile_dataframe()

    def run_prequal(self):
        cmd = ["prequal", self.pro_fasta]
        out = sp.run(cmd, capture_output=True)
        _log_process(out, program="prequal")
        self.pro_fasta = "{}.filtered".format(self.pro_fasta)

    def align(self):
        _write_fasta(self.pro_fasta, self.pro_seqs)
        if self.prequal:
            self.run_prequal()
        if self.aligner == "mafft":
            self.run_mafft(options=self.aln_options)
        else:
            logging.error("Unsupported aligner {}".format(self.aligner))
        self.get_codon_alignment()

    def run_mafft(self, options="--auto"):
        cmd = ["mafft"] + options.split() + ["--amino", self.pro_fasta]
        out = sp.run(cmd, capture_output=True)
        with open(self.pro_alnf, 'w') as f: f.write(out.stdout.decode('utf-8'))
        _log_process(out, program="mafft")
        self.pro_aln = AlignIO.read(self.pro_alnf, "fasta")

    def get_codon_alignment(self):
        self.cds_aln = _pal2nal(self.pro_aln, self.cds_seqs)
        if self.strip_gaps:
            self.cds_aln = _strip_gaps(self.cds_aln)
        _write_aln_codeml(self.cds_aln, self.cds_alnf)

    def run_codeml(self):
        codeml = Codeml(codeml="codeml", tmp=self.tmp_path, id=self.id)
        results_dict, codeml_out = codeml.run_codeml(os.path.basename(
            self.cds_alnf), preserve=True, times=self.codeml_iter)
        self.pairwise_estimates = results_dict

    def get_tree(self):
        # dispatch method
        if self.tree_method == "cluster":
            tree = self.cluster()
        elif self.tree_method == "iqtree":
            tree = self.run_iqtree(options=self.tree_options)
        self.tree = tree

    def run_iqtree(self, options="-m LG"):
        cmd = ["iqtree", "-s", self.pro_alnf] + options.split()
        out = sp.run(cmd, capture_output=True)
        _log_process(out, program="iqtree")
        tree = Phylo.read(self.pro_alnf + ".treefile", format="newick")
        _label_internals(tree)
        return tree

    def run_fasttree(self):
        pass

    def cluster(self):
        return _cluster(self.pairwise_estimates["Ks"])

    def compile_dataframe(self):
        n = len(self.cds_seqs)
        d = {}
        l = self.tree.get_terminals()
        for i in range(len(l)):
            gi = l[i].name
            for j in range(i+1, len(l)):
                gj = l[j].name
                pair = "__".join(sorted([gi, gj]))
                node = self.tree.common_ancestor(l[i], l[j])
                d[pair] = {"node": node.name, "family": self.id,
                    "gene1": gi, "gene2": gj}
                for k, v in self.pairwise_estimates.items():
                    d[pair][k] = v.loc[gi, gj]
        df = pd.DataFrame.from_dict(d, orient="index")
        df.to_csv(self.ks_out)


def _get_ks(family):
    family.get_ks()


class KsDistribution:
    def __init__(self, gene_families, n_threads=4):
        self.gene_families = gene_families
        self.df = None
        self.n_threads = n_threads

    def get_distribution(self):
        Parallel(n_jobs=self.n_threads)(
            delayed(_get_ks)(v) for v in self.gene_families.values())
        self.df = pd.concat([pd.read_csv(x.ks_out, index_col=0)
            for x in self.gene_families.values()])


# test
s = SequenceData("./_test/data/ath1000.fasta", tmp_path="_test/tmpdir")
s.get_paranome()
gfs = get_gene_families_paranome(s, s.mcl)
gfs = {i: gfs[i] for i in range(10,20)}
ks = KsDistribution(gfs)
ks.get_distribution()
ks.df
