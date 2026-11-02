// Use upstream syzkaller's typed program generator; never expand base syscall names.
package main

import (
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/syzkaller/pkg/csource"
	"github.com/google/syzkaller/prog"
	_ "github.com/google/syzkaller/sys"
)

//go:embed seeds/namespace.syz
var namespaceSeed []byte

//go:embed seeds/shared-contention.syz
var sharedContentionSeed []byte

//go:embed seeds/file-state.syz
var fileStateSeed []byte

var names = []string{
	"openat", "openat$dir", "close", "read", "write", "pwrite64",
	"fallocate", "ftruncate", "fsync", "fdatasync", "mkdirat", "rmdir",
	"unlinkat", "renameat2", "linkat", "symlinkat", "fchmod",
	"setxattr", "fsetxattr", "removexattr", "fremovexattr",
}

var dataCallNames = map[string]bool{
	"write": true, "pwrite64": true, "fallocate": true,
	"ftruncate": true, "fsync": true, "fdatasync": true,
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func main() {
	seed := flag.Int64("seed", 1, "generation/mutation seed")
	count := flag.Int("calls", 64, "maximum program calls")
	out := flag.String("out", "", "output directory")
	input := flag.String("mutate", "", "optional previous .syz program")
	mode := flag.String("input-mode", "ordinary", "ordinary or resource-valid")
	resourceTemplate := flag.String("resource-template", "auto", "auto, namespace, or file-state")
	flag.Parse()
	if *out == "" || *count < 1 || *count > 256 {
		must(fmt.Errorf("out and calls in [1,256] required"))
	}
	if *mode != "ordinary" && *mode != "resource-valid" {
		must(fmt.Errorf("unknown input mode %s", *mode))
	}
	if *resourceTemplate != "auto" && *resourceTemplate != "namespace" && *resourceTemplate != "shared-contention" && *resourceTemplate != "file-state" {
		must(fmt.Errorf("unknown resource template %s", *resourceTemplate))
	}
	resourceTemplateName := *resourceTemplate
	if *mode == "resource-valid" && (*count < 16 || *input != "") {
		must(fmt.Errorf("typed target modes require at least 16 calls and use their own seed"))
	}
	target, err := prog.GetTarget("linux", "amd64")
	must(err)
	enabled := make(map[*prog.Syscall]bool)
	for _, name := range names {
		call := target.SyscallMap[name]
		if call == nil {
			must(fmt.Errorf("missing exact syscall %s", name))
		}
		enabled[call] = true
	}
	usable, disabled := target.TransitivelyEnabledCalls(enabled)
	if len(disabled) != 0 {
		must(fmt.Errorf("disabled exact calls: %v", disabled))
	}
	ct := target.BuildChoiceTable(nil, usable)
	var p *prog.Prog
	var data, src []byte
	var lastErr error
	method := "Generate"
	resourcePrefixCalls := 0
	resourceHash := ""
	var resourcePrefix *prog.Prog
	if *mode == "resource-valid" {
		// Preserve typed resource dependencies while alternating between a
		// namespace operation sequence and a shorter data/extent state sequence.
		if resourceTemplateName == "auto" {
			if *seed%2 == 0 {
				resourceTemplateName = "namespace"
			} else {
				resourceTemplateName = "file-state"
			}
		}
		seedSource := namespaceSeed
		if resourceTemplateName == "shared-contention" {
			seedSource = sharedContentionSeed
		} else if resourceTemplateName == "file-state" {
			seedSource = fileStateSeed
		}
		seedText := string(seedSource)
		seedText = strings.ReplaceAll(seedText, "SEED", fmt.Sprintf("%d", *seed))
		seedText = strings.ReplaceAll(seedText, "linked", fmt.Sprintf("linked-%d", *seed))
		seedText = strings.ReplaceAll(seedText, "created-empty", fmt.Sprintf("empty-%d", *seed))
		resourcePrefix, err = target.Deserialize([]byte(seedText), prog.Strict)
		must(err)
		resourcePrefixCalls = len(resourcePrefix.Calls)
		hash := sha256.Sum256([]byte(seedText))
		resourceHash = fmt.Sprintf("%x", hash)
		method = "TypedSeedAndGeneratedTail"
		if *mode == "resource-valid" {
			method = "ResourceSeedAndGeneratedTail"
		}
	}
	attempts := 0
	acceptedAttempt := 0
	hasDataCall := false
	hasRegularOpenat := false
	inputData := []byte(nil)
	if *input != "" {
		inputData, err = os.ReadFile(*input)
		must(err)
	}
	// Mutate can occasionally produce an argument shape that the C exporter
	// rejects (for example an incomplete xattr union). Retry from the original
	// serialized parent so a bad attempt cannot poison the next one.
	build := func(mutating bool, baseSeed int64) bool {
		for attempt := 0; attempt < 64; attempt++ {
			attempts++
			rs := rand.NewSource(baseSeed + int64(attempt))
			if mutating {
				var decodeErr error
				p, decodeErr = target.Deserialize(inputData, prog.Strict)
				if decodeErr != nil {
					lastErr = decodeErr
					return false
				}
				for _, call := range p.Calls {
					if !enabled[call.Meta] {
						lastErr = fmt.Errorf("input call outside allowlist: %s", call.Meta.Name)
						return false
					}
				}
				p.Mutate(rs, *count, ct, nil, nil)
			} else {
				if resourcePrefix != nil {
					p = resourcePrefix.Clone()
					remaining := *count - resourcePrefixCalls
					if remaining > 0 {
						tail := target.Generate(rs, remaining, ct)
						tail.Mutate(rand.NewSource(baseSeed+int64(attempt)+500000), remaining, ct, nil, nil)
						p.Calls = append(p.Calls, tail.Calls...)
					}
				} else {
					p = target.Generate(rs, *count, ct)
				}
			}
			// syzkaller's call limit is a maximum, but the campaign's syscall
			// budget is an exact exposure count. Extend short programs with fresh
			// generated fragments before serialization and native-call indexing.
			for fill := 0; len(p.Calls) < *count && fill < 64; fill++ {
				remaining := *count - len(p.Calls)
				extra := target.Generate(rand.NewSource(baseSeed+int64(attempt)*1000+int64(fill)+900000), remaining, ct)
				if len(extra.Calls) == 0 {
					continue
				}
				p.Calls = append(p.Calls, extra.Calls...)
			}
			if len(p.Calls) > *count {
				lastErr = fmt.Errorf("program exceeded call budget")
				continue
			}
			if len(p.Calls) != *count {
				lastErr = fmt.Errorf("could not fill exact call budget: got %d want %d", len(p.Calls), *count)
				continue
			}
			data = p.Serialize()
			if _, lastErr = target.Deserialize(data, prog.Strict); lastErr != nil {
				continue
			}
			hasDataCall = false
			hasRegularOpenat = false
			for _, call := range p.Calls {
				hasDataCall = hasDataCall || dataCallNames[call.Meta.Name]
				hasRegularOpenat = hasRegularOpenat || call.Meta.Name == "openat"
			}
			if !hasDataCall {
				lastErr = fmt.Errorf("generated program has no data modification syscall")
				continue
			}
			src, lastErr = csource.Write(p, csource.Options{Procs: 1, Slowdown: 1, HandleSegv: true, Trace: true, CallComments: true})
			if lastErr == nil {
				acceptedAttempt = attempts
				return true
			}
		}
		return false
	}
	ok := build(*input != "", *seed)
	if !ok && *input != "" {
		// A malformed mutation must not terminate a long campaign. Keep the
		// parent in metadata, but use a fresh valid generated program for this slot.
		method = "GenerateFallback"
		ok = build(false, *seed+1000000)
	}
	if !ok {
		must(lastErr)
	}
	if *input != "" && method == "Generate" {
		method = "Mutate"
	}
	calls := make([]map[string]any, 0, len(p.Calls))
	for index, call := range p.Calls {
		if !enabled[call.Meta] {
			must(fmt.Errorf("generated call outside allowlist: %s", call.Meta.Name))
		}
		calls = append(calls, map[string]any{"index": index, "name": call.Meta.Name, "native_name": call.Meta.CallName, "nr": call.Meta.NR})
	}
	must(os.MkdirAll(*out, 0755))
	must(os.WriteFile(filepath.Join(*out, "program.syz"), data, 0644))
	must(os.WriteFile(filepath.Join(*out, "original.c"), src, 0644))
	meta, err := json.MarshalIndent(map[string]any{"schema": 1, "seed": *seed, "method": method,
		"attempts": attempts, "accepted_attempt": acceptedAttempt,
		"required_data_call": true, "has_data_call": hasDataCall,
		"has_regular_openat": hasRegularOpenat,
		"fd_repair_policy":   "stable_regular_file_fallback",
		"mutation_fallback":  method == "GenerateFallback", "target": "linux/amd64",
		"input_mode": *mode, "resource_contract": "private-worker-tree-v2",
		"resource_template": resourceTemplateName,
		"resource_prefix_calls": resourcePrefixCalls, "resource_seed_sha256": resourceHash,
		"protected_prefix": resourcePrefixCalls > 0, "namespace_parameter_adaptation": false,
		"call_limit": *count, "allowlist": names, "calls": calls}, "", "  ")
	must(err)
	must(os.WriteFile(filepath.Join(*out, "program.json"), append(meta, '\n'), 0644))
}
