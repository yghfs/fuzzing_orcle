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

func serializedCalls(p *prog.Prog) []string {
	text := strings.TrimSpace(string(p.Serialize()))
	if text == "" {
		return nil
	}
	return strings.Split(text, "\n")
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
	if *mode == "resource-valid" && *input != "" && resourceTemplateName == "auto" {
		metaPath := strings.TrimSuffix(*input, filepath.Ext(*input)) + ".json"
		metadata, readErr := os.ReadFile(metaPath)
		if readErr == nil {
			var parentMeta map[string]any
			must(json.Unmarshal(metadata, &parentMeta))
			if template, ok := parentMeta["resource_template"].(string); ok {
				resourceTemplateName = template
			}
		} else if !os.IsNotExist(readErr) {
			must(readErr)
		}
	}
	if *mode == "resource-valid" && *count < 16 {
		must(fmt.Errorf("typed target modes require at least 16 calls"))
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
	var seedLines []string
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
		seedLines = serializedCalls(resourcePrefix)
		hash := sha256.Sum256([]byte(seedText))
		resourceHash = fmt.Sprintf("%x", hash)
		method = "ResourceSeedMutate"
	}
	attempts := 0
	acceptedAttempt := 0
	hasDataCall := false
	hasRegularOpenat := false
	inputData := []byte(nil)
	var mutationBaseLines []string
	parentSeedHash := ""
	parentPrefixCalls := 0
	parentSeedCalls := 0
	if *input != "" {
		inputData, err = os.ReadFile(*input)
		must(err)
		if *mode == "resource-valid" {
			metaPath := strings.TrimSuffix(*input, filepath.Ext(*input)) + ".json"
			if metadata, readErr := os.ReadFile(metaPath); readErr == nil {
				var parentMeta map[string]any
				must(json.Unmarshal(metadata, &parentMeta))
				if parentMeta["input_mode"] != "resource-valid" ||
					parentMeta["resource_template"] != resourceTemplateName {
					must(fmt.Errorf("resource parent template/metadata mismatch"))
				}
				if value, ok := parentMeta["resource_seed_sha256"].(string); ok {
					parentSeedHash = value
				}
				if value, ok := parentMeta["resource_prefix_calls"].(float64); ok {
					parentPrefixCalls = int(value)
				}
				if value, ok := parentMeta["resource_seed_calls"].(float64); ok {
					parentSeedCalls = int(value)
				}
			} else if !os.IsNotExist(readErr) {
				must(readErr)
			}
			parent, decodeErr := target.Deserialize(inputData, prog.Strict)
			must(decodeErr)
			mutationBaseLines = serializedCalls(parent)
			method = "ResourceMutate"
		}
	}
	independentFillStart := *count
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
					mutationBaseLines = serializedCalls(p)
					p.Mutate(rand.NewSource(baseSeed+int64(attempt)+500000), *count, ct, nil, nil)
				} else {
					p = target.Generate(rs, *count, ct)
				}
			}
			// syzkaller's call limit is a maximum, but the campaign's syscall
			// budget is an exact exposure count. Extend short programs with fresh
			// generated fragments before serialization and native-call indexing.
			independentFillStart = len(p.Calls)
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
	if parentSeedHash != "" && method != "GenerateFallback" {
		resourceHash = parentSeedHash
	}
	usedParent := *input != "" && method != "GenerateFallback"
	freshGenerate := *input == "" && resourcePrefix == nil && method != "GenerateFallback"
	finalLines := serializedCalls(p)
	callOrigins := make([]string, len(p.Calls))
	seedRetained := 0
	resourcePrefixRetained := 0
	mutableCalls := 0
	for index := range p.Calls {
		switch {
		case index >= independentFillStart:
			callOrigins[index] = "independent_fill"
		case freshGenerate:
			callOrigins[index] = "fresh_generate"
		case index < len(mutationBaseLines) && finalLines[index] == mutationBaseLines[index]:
			if !usedParent {
				callOrigins[index] = "seed_retained"
				seedRetained++
			} else {
				callOrigins[index] = "parent_retained"
			}
		case index < len(seedLines) && !usedParent && finalLines[index] == seedLines[index]:
			callOrigins[index] = "seed_retained"
			seedRetained++
		default:
			callOrigins[index] = "typed_mutation"
		}
		if callOrigins[index] == "typed_mutation" {
			mutableCalls++
		}
	}
	prefixLines := seedLines
	prefixLimit := len(seedLines)
	if usedParent {
		prefixLines = mutationBaseLines
		prefixLimit = parentPrefixCalls
		if parentSeedCalls > 0 {
			resourcePrefixCalls = parentSeedCalls
		}
	}
	if prefixLimit > len(prefixLines) {
		prefixLimit = len(prefixLines)
	}
	for index := 0; index < prefixLimit && index < len(finalLines); index++ {
		if finalLines[index] != prefixLines[index] {
			break
		}
		resourcePrefixRetained++
	}
	calls := make([]map[string]any, 0, len(p.Calls))
	for index, call := range p.Calls {
		if !enabled[call.Meta] {
			must(fmt.Errorf("generated call outside allowlist: %s", call.Meta.Name))
		}
		calls = append(calls, map[string]any{"index": index, "name": call.Meta.Name, "native_name": call.Meta.CallName, "nr": call.Meta.NR, "origin": callOrigins[index]})
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
		"resource_template":            resourceTemplateName,
		"resource_seed_calls":          resourcePrefixCalls,
		"resource_prefix_calls":        resourcePrefixRetained,
		"resource_seed_retained_calls": seedRetained,
		"resource_mutable_calls":       mutableCalls,
		"resource_prefix_fraction":     float64(resourcePrefixRetained) / float64(len(p.Calls)),
		"resource_seed_sha256":         resourceHash,
		"resource_prefix_mutable":      true, "protected_prefix": false,
		"namespace_parameter_adaptation": false, "call_origins": callOrigins,
		"call_limit": *count, "allowlist": names, "calls": calls}, "", "  ")
	must(err)
	must(os.WriteFile(filepath.Join(*out, "program.json"), append(meta, '\n'), 0644))
}
