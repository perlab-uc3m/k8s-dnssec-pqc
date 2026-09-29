// verify checks captured private-zone DNSSEC responses against a pinned DNSKEY.
// It runs after the measured workload so verifier CPU does not affect DNS timing.
package main

import (
	"bufio"
	"compress/gzip"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/miekg/dns"
)

type query struct {
	ID        uint64   `json:"query_id"`
	DNSID     *uint16  `json:"dns_id"`
	FQDN      string   `json:"fqdn"`
	Status    string   `json:"status"`
	Completed *float64 `json:"completed_s"`
	Attempts  []any    `json:"attempts"`
}
type load struct {
	UTCAnchor float64 `json:"utc_anchor"`
}
type result struct {
	ID           uint64 `json:"query_id"`
	Verification string `json:"verification"`
	Error        string `json:"error,omitempty"`
}

func openGzip(path string) (*gzip.Reader, *os.File, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	z, err := gzip.NewReader(f)
	if err != nil {
		f.Close()
		return nil, nil, err
	}
	return z, f, nil
}

func readFrame(reader io.Reader, id uint64, expectedAttempt int) ([]byte, error) {
	var header [13]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return nil, err
	}
	gotID := binary.BigEndian.Uint64(header[0:8])
	if gotID != id || int(header[8]) != expectedAttempt {
		return nil, fmt.Errorf("raw frame mismatch: got query %d attempt %d; expected %d/%d", gotID, header[8], id, expectedAttempt)
	}
	n := binary.BigEndian.Uint32(header[9:13])
	if n > 65535 {
		return nil, fmt.Errorf("oversized DNS frame: %d", n)
	}
	data := make([]byte, n)
	_, err := io.ReadFull(reader, data)
	return data, err
}

func trustedKey(path string) (*dns.DNSKEY, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	rr, err := dns.ReadRR(f, path)
	if err != nil {
		return nil, err
	}
	key, ok := rr.(*dns.DNSKEY)
	if !ok {
		return nil, fmt.Errorf("%s is not a DNSKEY", path)
	}
	if key.Protocol != 3 || key.Flags&256 == 0 {
		return nil, fmt.Errorf("pinned DNSKEY is not a protocol 3 zone key")
	}
	return key, nil
}

func verifyPositive(wire []byte, q query, key *dns.DNSKEY, captured time.Time) error {
	var msg dns.Msg
	if err := msg.Unpack(wire); err != nil {
		return err
	}
	if !msg.Response || msg.Opcode != dns.OpcodeQuery || len(msg.Question) != 1 ||
		msg.Question[0].Qtype != dns.TypeA || msg.Question[0].Qclass != dns.ClassINET ||
		!strings.EqualFold(msg.Question[0].Name, q.FQDN) ||
		(q.DNSID != nil && msg.Id != *q.DNSID) {
		return fmt.Errorf("response header or question does not match request")
	}
	if msg.Rcode != dns.RcodeSuccess || msg.Truncated {
		return fmt.Errorf("response rcode %d or truncated", msg.Rcode)
	}
	type rrkey struct {
		name        string
		class, kind uint16
	}
	sets := make(map[rrkey][]dns.RR)
	sigs := make(map[rrkey][]*dns.RRSIG)
	foundA := false
	for _, rr := range msg.Answer {
		h := rr.Header()
		if s, ok := rr.(*dns.RRSIG); ok {
			k := rrkey{strings.ToLower(h.Name), h.Class, s.TypeCovered}
			sigs[k] = append(sigs[k], s)
			continue
		}
		k := rrkey{strings.ToLower(h.Name), h.Class, h.Rrtype}
		sets[k] = append(sets[k], rr)
		if h.Rrtype == dns.TypeA && strings.EqualFold(h.Name, q.FQDN) {
			foundA = true
		}
	}
	if !foundA {
		return fmt.Errorf("expected A RRset absent")
	}
	for k, rrset := range sets {
		valid := false
		for _, sig := range sigs[k] {
			if sig.Algorithm != key.Algorithm || sig.KeyTag != key.KeyTag() ||
				!strings.EqualFold(sig.SignerName, key.Hdr.Name) ||
				!strings.EqualFold(sig.Hdr.Name, k.name) {
				continue
			}
			if !sig.ValidityPeriod(captured) {
				continue
			}
			if sig.Verify(key, rrset) == nil {
				valid = true
				break
			}
		}
		if !valid {
			return fmt.Errorf("missing or invalid RRSIG for %s type %d", k.name, k.kind)
		}
	}
	return nil
}

func run(runDir, keyPath string) error {
	key, err := trustedKey(keyPath)
	if err != nil {
		return err
	}
	metaData, err := os.ReadFile(filepath.Join(runDir, "load.json"))
	if err != nil {
		return err
	}
	var meta load
	if err := json.Unmarshal(metaData, &meta); err != nil {
		return err
	}
	qz, qf, err := openGzip(filepath.Join(runDir, "queries.jsonl.gz"))
	if err != nil {
		return err
	}
	defer qz.Close()
	defer qf.Close()
	rz, rf, err := openGzip(filepath.Join(runDir, "responses.bin.gz"))
	if err != nil {
		return err
	}
	defer rz.Close()
	defer rf.Close()
	out, err := os.Create(filepath.Join(runDir, "verification.jsonl"))
	if err != nil {
		return err
	}
	defer out.Close()
	writer := bufio.NewWriter(out)
	scanner := bufio.NewScanner(qz)
	scanner.Buffer(make([]byte, 65536), 4*1024*1024)
	count, verified, invalid := 0, 0, 0
	for scanner.Scan() {
		var q query
		if err := json.Unmarshal(scanner.Bytes(), &q); err != nil {
			return err
		}
		var final []byte
		for attempt := range q.Attempts {
			final, err = readFrame(rz, q.ID, attempt)
			if err != nil {
				return err
			}
		}
		r := result{ID: q.ID, Verification: "not_applicable"}
		switch q.Status {
		case "positive_unverified":
			if q.Completed == nil {
				return fmt.Errorf("missing completion time for query %d", q.ID)
			}
			captured := time.Unix(0, int64((meta.UTCAnchor+*q.Completed)*1e9))
			if err := verifyPositive(final, q, key, captured); err != nil {
				r.Verification, r.Error = "invalid", err.Error()
				invalid++
			} else {
				r.Verification = "private_zone_signature_verified"
				verified++
			}
		case "negative_unverified", "empty_unverified":
			r.Verification = "negative_unvalidated"
		}
		bytes, _ := json.Marshal(r)
		if _, err := writer.Write(append(bytes, byte(10))); err != nil {
			return err
		}
		count++
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	var extra [1]byte
	if n, _ := rz.Read(extra[:]); n != 0 {
		return fmt.Errorf("extra response frames remain")
	}
	if err := writer.Flush(); err != nil {
		return err
	}
	summary, _ := json.MarshalIndent(map[string]any{
		"query_count": count, "private_zone_signature_verified": verified,
		"invalid_positive": invalid, "negative_proofs": "not_validated",
		"key_algorithm": key.Algorithm, "key_tag": key.KeyTag(),
	}, "", "  ")
	return os.WriteFile(filepath.Join(runDir, "verification.json"), append(summary, byte(10)), 0644)
}

func main() {
	runDir := flag.String("run-dir", "", "run artifact directory")
	key := flag.String("key", "", "pinned public DNSKEY file")
	flag.Parse()
	if *runDir == "" || *key == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := run(*runDir, *key); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
